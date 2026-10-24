# skill: skill_config
# description: 给指定 skill 添加配置项：用户提供键值，自动写入 config.json、config.example.json 与 index.json
# status: enable
# usage:
#   arguments.skill = 目标技能名（必填）
#   arguments.config = 用户提供的配置键值对象（add 必填），如 {"MYSQL_CHARSET": "utf8mb4"}
#   arguments.action = add（默认，写配置）| show（查看，值打码）| remove | sync | wire
#   arguments.example / keys / confirm / repo = 模板值 / 待删键 / 写操作确认 / 仓库根（可选）
#   说明: 只写入用户给的值，不生成值；config.json 已 gitignore，返回打码。配置键为环境变量式大写名。
#   示例: {"action": "add", "skill": "mysql", "config": {"MYSQL_CHARSET": "utf8mb4"}}
#   示例: {"action": "show", "skill": "mysql"}
import ast
import json
import os
import re
import sys

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    try:
        from core.tools.envelope import ok, error
    except ImportError:  # 本地调试：python skill.py '{...}'
        def ok(result=None):
            return {"status": "ok", "result": result}

        def error(message, code="internal"):
            return {"status": "error", "error": message, "code": code}

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
SKILL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LOADER_NAME = "_load_config"

LOADER_SRC = '''def _load_config():
    """把本技能文件夹内的 config.json 注入环境变量（不覆盖已有 env）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(cfg, dict):
        for key, value in cfg.items():
            if value and not os.environ.get(key):
                os.environ[key] = str(value)



'''


# ---------------------------------------------------------------- 基础工具

def _skills_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _skill_dir(name, skills_root=None):
    if not isinstance(name, str) or not SKILL_RE.match(name.strip()):
        raise ValueError("skill 名非法：只允许字母数字与 _ . -")
    name = name.strip()
    root = os.path.realpath(skills_root or _skills_root())
    target = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(target) != root:
        raise ValueError("skill 名不能包含路径")
    if not os.path.isdir(target) or not os.path.isfile(os.path.join(target, "skill.py")):
        raise ValueError("技能不存在或缺少 skill.py: %s" % target)
    return target


def _find_repo(start=None, explicit=None):
    if explicit:
        root = os.path.realpath(os.path.expanduser(str(explicit)))
        if os.path.isfile(os.path.join(root, "index.json")):
            return root
        raise ValueError("repo 目录下没有 index.json: %s" % root)
    cur = os.path.realpath(start or _skills_root())
    for _ in range(4):
        idx = os.path.join(cur, "index.json")
        if os.path.isfile(idx):
            try:
                with open(idx, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("skills"), list):
                return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError("读取 %s 失败: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ValueError("%s 顶层必须是 JSON 对象" % path)
    return data


def _dumps_inline(data, level=0, ensure_ascii=False):
    """仓库风格：对象多行、标量数组内联（如 ["linux", "windows"]）。"""
    pad = "  " * level
    pad_in = "  " * (level + 1)
    if isinstance(data, dict):
        if not data:
            return "{}"
        items = ",\n".join(
            "%s%s: %s" % (pad_in, json.dumps(k, ensure_ascii=ensure_ascii),
                          _dumps_inline(v, level + 1, ensure_ascii))
            for k, v in data.items())
        return "{\n%s\n%s}" % (items, pad)
    if isinstance(data, list):
        if not data:
            return "[]"
        if all(not isinstance(item, (dict, list)) for item in data):
            return "[" + ", ".join(
                json.dumps(item, ensure_ascii=ensure_ascii) for item in data) + "]"
        inner = ",\n".join(
            pad_in + _dumps_inline(item, level + 1, ensure_ascii) for item in data)
        return "[\n%s\n%s]" % (inner, pad)
    return json.dumps(data, ensure_ascii=ensure_ascii)


def _detect_style(path):
    """识别既有文件的排版：返回 ("inline", ensure_ascii) 或 (缩进, ensure_ascii)。"""
    try:
        raw = open(path, encoding="utf-8").read().rstrip("\n")
        data = json.loads(raw)
    except (OSError, ValueError):
        return "inline", False
    for ascii_ in (False, True):
        if _dumps_inline(data, 0, ascii_) == raw:
            return "inline", ascii_
    for indent in (2, 4):
        for ascii_ in (False, True):
            if json.dumps(data, ensure_ascii=ascii_, indent=indent) == raw:
                return indent, ascii_
    return "inline", False


def _write_json(path, data):
    style, ascii_ = _detect_style(path)
    if style == "inline":
        text = _dumps_inline(data, 0, ascii_) + "\n"
    else:
        text = json.dumps(data, ensure_ascii=ascii_, indent=style) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return style


def _norm_value(value):
    """把用户给的值规范成字符串（loader 会把值 str() 后注入环境变量）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise ValueError("配置值只支持字符串/数字/布尔/null")


def _mask(value):
    text = str(value)
    if not text:
        return "(空)"
    if len(text) <= 8:
        return "******"
    return text[:2] + "***"


def _is_flat_map(data):
    """config.example.json 是否为「键 -> 字符串」的扁平配置。"""
    return all(isinstance(v, str) for v in data.values())


def _loader_state(skill_dir):
    path = os.path.join(skill_dir, "skill.py")
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return False, False
    has_def = re.search(r"^def\s+%s\s*\(" % LOADER_NAME, src, re.M) is not None
    has_call = re.search(r"(?<![\w.])%s\s*\(" % LOADER_NAME, src) is not None and has_def
    return has_def, has_call


# ---------------------------------------------------------------- index.json

def _index_entry(index, skill):
    for entry in index.get("skills", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("name") == skill or entry.get("path") == skill:
            return entry
    return None


def _update_index(repo, skill, add_keys=(), remove_keys=(), rebuild=None):
    """返回 (变化描述, 警告列表)。repo 为 None 时跳过。"""
    if not repo:
        return None, ["未找到仓库 index.json，已跳过索引更新（可用 repo 参数指定）"]
    path = os.path.join(repo, "index.json")
    index = _read_json(path)
    entry = _index_entry(index, skill)
    if entry is None:
        return None, ["index.json 中没有 %s 条目，已跳过索引更新" % skill]
    current = entry.get("config")
    if not isinstance(current, list):
        current = []
    before = list(current)
    if rebuild is not None:
        current = list(rebuild)
    else:
        for key in add_keys:
            if key not in current:
                current.append(key)
        for key in remove_keys:
            current = [item for item in current if item != key]
    if current == before:
        return None, []
    entry["config"] = current
    _write_json(path, index)
    return {
        "before": before,
        "after": current,
        "added": [k for k in current if k not in before],
        "removed": [k for k in before if k not in current],
    }, []


# ---------------------------------------------------------------- 写配置

def _merge_config(skill_dir, config, example=None):
    """写入 config.json（真实值）与 config.example.json（模板值）。

    返回 (report, config_keys)。已存在的其它键原样保留。
    """
    config = config or {}
    example = example or {}
    if not isinstance(config, dict) or not config:
        raise ValueError("config 必须是非空对象，且配置值由用户提供")
    for key in config:
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise ValueError("非法配置键 %r：需为大写环境变量名（如 MYSQL_CHARSET）" % key)
    for key in (example or {}):
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise ValueError("非法示例键 %r：需为大写环境变量名" % key)

    report = {}

    real_path = os.path.join(skill_dir, "config.json")
    real = _read_json(real_path) or {}
    real_before = dict(real)
    for key, value in config.items():
        real[key] = _norm_value(value)
    if real != real_before:
        _write_json(real_path, real)
    report["config.json"] = {
        "path": real_path,
        "added": [k for k in real if k not in real_before],
        "updated": [k for k in real if k in real_before and real[k] != real_before[k]],
    }

    example_path = os.path.join(skill_dir, "config.example.json")
    tpl = _read_json(example_path) or {}
    tpl_before = dict(tpl)
    for key in config:
        if key in tpl:
            if key in example:
                tpl[key] = _norm_value(example[key])
        else:
            tpl[key] = _norm_value(example.get(key, ""))
    for key in example:
        if key not in tpl:
            tpl[key] = _norm_value(example[key])
    if tpl != tpl_before:
        _write_json(example_path, tpl)
    report["config.example.json"] = {
        "path": example_path,
        "added": [k for k in tpl if k not in tpl_before],
        "updated": [k for k in tpl if k in tpl_before and tpl[k] != tpl_before[k]],
    }
    return report, list(real.keys())


def _wire_skill(skill_dir, confirm):
    """给 skill.py 注入 _load_config 及 run() 内的调用。返回报告。"""
    path = os.path.join(skill_dir, "skill.py")
    src = open(path, encoding="utf-8").read()
    has_def, has_call = _loader_state(skill_dir)
    if has_def and has_call:
        return {"changed": False, "reason": "已存在 _load_config 且已调用"}
    if not confirm:
        return {
            "changed": False,
            "planned": True,
            "reason": "需要 confirm=true 才写入 skill.py",
            "need_def": not has_def,
            "need_call": not has_call,
        }

    original = src
    if not has_def:
        if not re.search(r"^\s*import\s+os\b", src, re.M) and not re.search(
                r"^\s*from\s+os\s+import\b", src, re.M):
            src = _insert_imports(src, ["import os"])
        if not re.search(r"^\s*import\s+json\b", src, re.M) and not re.search(
                r"^\s*from\s+json\s+import\b", src, re.M):
            src = _insert_imports(src, ["import json"])
        match = re.search(r"^def\s+run\s*\(", src, re.M)
        if not match:
            return {"changed": False, "reason": "未找到 def run(...)，需手动接入"}
        src = src[:match.start()] + LOADER_SRC + src[match.start():]

    if not has_call:
        src = _insert_loader_call(src)
        if src is None:
            return {"changed": False, "reason": "未能在 run() 内定位插入点，需手动接入"}

    try:
        compile(src, path, "exec")
    except SyntaxError as exc:
        return {"changed": False, "reason": "注入后语法校验失败，已放弃：%s" % exc}

    if src != original:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
    return {
        "changed": src != original,
        "added_def": not has_def,
        "added_call": not has_call,
        "path": path,
    }


def _insert_imports(src, imports):
    lines = src.splitlines(keepends=True)
    cut = 0
    for i, line in enumerate(lines):
        if line.startswith("#"):
            cut = i + 1
        elif line.strip():
            break
    block = "".join(item + "\n" for item in imports)
    nxt = lines[cut] if cut < len(lines) else ""
    if nxt.strip() and not nxt.lstrip().startswith(("import ", "from ")):
        block += "\n"
    lines.insert(cut, block)
    return "".join(lines)


def _insert_loader_call(src):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    run_node = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run_node = node
            break
    if run_node is None or not run_node.body:
        return None

    body = run_node.body
    first = body[0]
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        if len(body) > 1:
            target = body[1]
        else:
            # 只有 docstring：插在 docstring 之后
            lines = src.splitlines(keepends=True)
            indent = " " * (run_node.col_offset + 4)
            lines.insert(first.end_lineno, indent + LOADER_NAME + "()\n")
            return "".join(lines)
    else:
        target = first

    lines = src.splitlines(keepends=True)
    line = lines[target.lineno - 1]
    indent = line[:len(line) - len(line.lstrip())] or " " * (run_node.col_offset + 4)
    lines.insert(target.lineno - 1, indent + LOADER_NAME + "()\n")
    return "".join(lines)


# ---------------------------------------------------------------- actions

def _resolve_target(a):
    """确定要改的 skill 目录与仓库根。

    显式/自动找到仓库且仓库里有同名技能时，优先改仓库副本（便于从运行时维护仓库）；
    否则改当前技能目录树里的同名技能。
    """
    repo = _find_repo(explicit=a.get("repo"))
    name = a.get("skill")
    root = None
    if repo and isinstance(name, str) and os.path.isfile(
            os.path.join(repo, name, "skill.py")):
        root = repo
    return _skill_dir(name, skills_root=root), repo


def _action_add(a):
    skill_dir, repo = _resolve_target(a)
    existing_example = _read_json(
        os.path.join(skill_dir, "config.example.json")) or {}
    report, _ = _merge_config(
        skill_dir, a.get("config"), a.get("example") or {})
    changed_keys = [k for k in (a.get("config") or {})]

    index_report, warnings = _update_index(
        repo, os.path.basename(skill_dir), add_keys=changed_keys)
    if existing_example and not _is_flat_map(existing_example):
        warnings.append(
            "该技能原有 config.example.json 不是扁平键值结构（含嵌套对象/非字符串），"
            "本技能只追加顶层键，请确认其代码确实按这些键读取")

    has_def, has_call = _loader_state(skill_dir)
    wire_report = None
    if a.get("wire"):
        wire_report = _wire_skill(skill_dir, confirm=True)
        if not wire_report.get("changed"):
            warnings.append("wire 未生效：%s" % wire_report.get("reason"))
        has_def, has_call = _loader_state(skill_dir)
    elif not has_call:
        warnings.append(
            "该技能尚未调用 _load_config()，配置写入后不会自动生效；"
            "可执行 action=wire confirm=true 注入读取逻辑（配置若由环境变量提供则忽略此项）")

    result = {
        "skill": os.path.basename(skill_dir),
        "skill_dir": skill_dir,
        "repo": repo,
        "keys": changed_keys,
        "files": report,
        "index": index_report,
        "code_reads_config": bool(has_def and has_call),
        "warnings": warnings,
    }
    if wire_report is not None:
        result["wire"] = wire_report
    return ok(result)


def _action_show(a):
    skill_dir, repo = _resolve_target(a)

    config = _read_json(os.path.join(skill_dir, "config.json")) or {}
    example = _read_json(os.path.join(skill_dir, "config.example.json")) or {}
    index_keys = []
    if repo:
        index = _read_json(os.path.join(repo, "index.json"))
        entry = _index_entry(index, os.path.basename(skill_dir))
        if entry and isinstance(entry.get("config"), list):
            index_keys = list(entry["config"])

    keys = list(example.keys())
    for key in list(config.keys()) + index_keys:
        if key not in keys:
            keys.append(key)

    has_def, has_call = _loader_state(skill_dir)
    items = []
    for key in keys:
        value = config.get(key, "")
        items.append({
            "key": key,
            "set": bool(str(value).strip()) if value is not None else False,
            "value": _mask(value) if key in config else "(未写入 config.json)",
            "in_example": key in example,
            "in_index": key in index_keys,
        })

    return ok({
        "skill": os.path.basename(skill_dir),
        "skill_dir": skill_dir,
        "repo": repo,
        "config_file": os.path.join(skill_dir, "config.json") if config else None,
        "code_reads_config": bool(has_def and has_call),
        "flat_example": _is_flat_map(example),
        "keys": items,
        "drift": {
            "example_not_in_index": [k for k in example if k not in index_keys] if repo else [],
            "index_not_in_example": [k for k in index_keys if k not in example] if repo else [],
            "config_not_in_example": [k for k in config if k not in example],
        },
    })


def _action_remove(a):
    if a.get("confirm") is not True:
        return error("删除配置项会改动文件，需传 confirm=true", code="need_confirm")
    skill_dir, repo = _resolve_target(a)
    keys = a.get("keys") or []
    if not isinstance(keys, list) or not keys:
        return error("keys 必须是非空数组", code="invalid_argument")
    for key in keys:
        if not isinstance(key, str) or not KEY_RE.match(key):
            return error("非法配置键 %r" % key, code="invalid_argument")

    repo = _find_repo(explicit=a.get("repo"))
    removed = {}
    for name, path in (("config.json", os.path.join(skill_dir, "config.json")),
                       ("config.example.json", os.path.join(skill_dir, "config.example.json"))):
        data = _read_json(path)
        if data is None:
            continue
        hit = [k for k in keys if k in data]
        for key in hit:
            data.pop(key, None)
        if hit:
            _write_json(path, data)
        removed[name] = hit

    index_report, warnings = _update_index(
        repo, os.path.basename(skill_dir), remove_keys=keys)
    return ok({
        "skill": os.path.basename(skill_dir),
        "removed": removed,
        "index": index_report,
        "warnings": warnings,
    })


def _action_sync(a):
    skill_dir, repo = _resolve_target(a)
    example = _read_json(os.path.join(skill_dir, "config.example.json"))
    if example is None:
        return error("该技能没有 config.example.json，无从同步", code="not_found")
    if not _is_flat_map(example):
        return error("config.example.json 不是扁平键值结构（含嵌套对象/非字符串），"
                     "拒绝用其重建索引；请手动维护 index.json", code="not_flat")
    if a.get("confirm") is not True:
        return error("sync 会重建 index.json 的 config 列表，需传 confirm=true",
                     code="need_confirm")
    index_report, warnings = _update_index(
        repo, os.path.basename(skill_dir), rebuild=list(example.keys()))
    return ok({
        "skill": os.path.basename(skill_dir),
        "index": index_report,
        "warnings": warnings,
    })


def _action_wire(a):
    skill_dir, _ = _resolve_target(a)
    report = _wire_skill(skill_dir, confirm=a.get("confirm") is True)
    if report.get("changed"):
        has_def, has_call = _loader_state(skill_dir)
        report["code_reads_config"] = bool(has_def and has_call)
    return ok({"skill": os.path.basename(skill_dir), "wire": report})


ACTIONS = {
    "add": _action_add,
    "show": _action_show,
    "remove": _action_remove,
    "sync": _action_sync,
    "wire": _action_wire,
}


def run(arguments):
    _load = globals().get(LOADER_NAME)
    if callable(_load):
        _load()
    a = arguments if isinstance(arguments, dict) else {}
    action = str(a.get("action") or "add").strip().lower()
    handler = ACTIONS.get(action)
    if handler is None:
        return error("未知 action '%s'，可用: %s" % (action, " / ".join(ACTIONS)),
                     code="unknown_action")
    try:
        return handler(a)
    except ValueError as exc:
        return error(str(exc), code="invalid_argument")
    except OSError as exc:
        return error("文件操作失败: %s" % exc, code="io_error")


if __name__ == "__main__":  # 本地调试：python skill.py '{...}'
    print(json.dumps(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}),
                     ensure_ascii=False, indent=2))
