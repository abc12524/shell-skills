# skill: skill_config
# description: 技能配置助手：传技能名返回待配的键，给值后写回该技能 config.json 并返回文件全文（明文）
# status: enable
# usage:
#   arguments.action = keys（默认，列待配键）| set（写回）| remove（删键）
#   arguments.skill = 目标技能名（必填）；arguments.repo = 仓库根（可选，默认向上找 index.json）
#   set: arguments.config = {键: 值}，增量合并——只覆盖传入的键，可只配一项或几项
#   remove: arguments.keys = [键名]
#   待配键取自 config.example.json，缺失时用 index.json 登记键；值明文保存、不打码
#   示例: {"skill": "baidu_search"}
#   示例: {"action": "set", "skill": "baidu_search", "config": {"BAIDU_QIANFAN_KEY": "bce-v3-xxx"}}
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

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")  # 常规是大写环境变量名
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
CONFIG_NAME = "config.json"
EXAMPLE_NAME = "config.example.json"


def _skills_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_repo(explicit=None):
    """显式 repo 优先；否则从技能目录向上找带 index.json 的目录。"""
    if explicit:
        root = os.path.realpath(os.path.expanduser(str(explicit)))
        if not os.path.isfile(os.path.join(root, "index.json")):
            raise ValueError("repo 目录下没有 index.json: %s" % root)
        return root
    cur = os.path.realpath(_skills_root())
    for _ in range(4):
        if os.path.isfile(os.path.join(cur, "index.json")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _skill_dir(name, repo):
    if not isinstance(name, str) or not NAME_RE.match(name.strip()):
        raise ValueError("skill 名非法：只允许字母数字与 _ . -")
    name = name.strip()
    root = os.path.realpath(repo or _skills_root())
    target = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(target) != root:
        raise ValueError("skill 名不能包含路径")
    if not os.path.isfile(os.path.join(target, "skill.py")):
        raise ValueError("技能不存在或缺少 skill.py: %s" % target)
    return target


def _resolve(a):
    repo = _find_repo(a.get("repo"))
    return _skill_dir(a.get("skill"), repo), repo


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError("读取 %s 失败: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ValueError("%s 顶层必须是 JSON 对象" % path)
    return data


def _write_json(path, data):
    """写回并返回文件全文（返回值直接给用户看）。"""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return text


def _filled(value):
    if isinstance(value, (dict, list)):
        return bool(value)
    return value is not None and str(value).strip() != ""


def _reads_config(skill_dir):
    """粗判该技能是否会读 config.json（有 _load_config 或提到 config.json 即算）。"""
    try:
        src = open(os.path.join(skill_dir, "skill.py"), encoding="utf-8").read()
    except OSError:
        return False
    return "_load_config" in src or CONFIG_NAME in src


def _index_keys(repo, name):
    """仓库索引里登记的该技能配置键（模板缺失时兜底用）。"""
    if not repo:
        return []
    try:
        index = _read_json(os.path.join(repo, "index.json"))
    except ValueError:
        return []
    for entry in index.get("skills", []):
        if isinstance(entry, dict) and (entry.get("name") == name or entry.get("path") == name):
            keys = entry.get("config")
            return list(keys) if isinstance(keys, list) else []
    return []


def _candidates(skill_dir, repo, config, example):
    """待配键来源：config.example.json 优先，没有就用 index.json 登记键；再补 config.json 里已有的。"""
    if example:
        source, keys = EXAMPLE_NAME, list(example)
    else:
        source, keys = "index.json", _index_keys(repo, os.path.basename(skill_dir))
    extra = [k for k in config if k not in keys]
    return source, keys, extra


def _hints(skill_dir, items):
    out = []
    if not _reads_config(skill_dir):
        out.append("该技能的 skill.py 没发现读取 config.json 的代码：写入后不会生效，"
                   "需在 run() 开头调用 _load_config()（写法参见 skill_guide）")
    complex_keys = [i["key"] for i in items if i["complex"]]
    if complex_keys:
        out.append("键 %s 的值是对象/数组，本技能按整体替换写入；复杂结构建议直接编辑 config.json"
                   % ", ".join(complex_keys))
    out.append("config.json 已 gitignore，值以明文保存，注意别提交或外传")
    return out


def _keys_report(a):
    skill_dir, repo = _resolve(a)
    path = os.path.join(skill_dir, CONFIG_NAME)
    config = _read_json(path)
    example = _read_json(os.path.join(skill_dir, EXAMPLE_NAME))
    source, keys, extra = _candidates(skill_dir, repo, config, example)

    items = []
    for key in keys + extra:
        value = config.get(key)
        placeholder = example.get(key)
        complex_ = isinstance(value, (dict, list)) or isinstance(placeholder, (dict, list))
        ok_ = _filled(value)
        items.append({
            "key": key,
            "need": not ok_,
            "value": value if ok_ else "",
            "placeholder": example[key] if key in example else "",
            "complex": complex_,
            "in_example": key in example,
        })

    to_fill = [i["key"] for i in items if i["need"]]
    result = {
        "skill": os.path.basename(skill_dir),
        "file": path if config else None,
        "keys_source": source,
        "to_fill": to_fill,
        "keys": items,
        "hints": _hints(skill_dir, items),
        "next": ("可只问其中任意几项（不必全给），拿到值后 action=set + config={键: 值} 写回："
                 "只覆盖传入的键，%s 等其余键保持原样" % (to_fill[0] if to_fill else "其它")
                 if to_fill else
                 "配置已齐；若要改某一项，直接 action=set 传该键的新值即可（只覆盖该项）"),
    }
    if not items:
        result["next"] = ("该技能没有 %s，也没有 index.json 登记键：建议手动建 config.example.json，"
                          "或直接用 action=set 指定 config" % EXAMPLE_NAME)
    return ok(result)


def _norm(value):
    """标量转字符串（读配置方一般按字符串用）；对象/数组原样保留。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, (dict, list)):
        return value
    raise ValueError("配置值只支持字符串 / 数字 / 布尔 / null / 对象 / 数组")


def _action_set(a):
    config = a.get("config")
    if not isinstance(config, dict) or not config:
        return error("config 必须是非空对象，如 {\"BAIDU_QIANFAN_KEY\": \"bce-v3-xxx\"}",
                     code="invalid_argument")
    for key in config:
        if not isinstance(key, str) or not KEY_RE.match(key):
            return error("非法配置键 %r：只允许字母数字与 _ . -，常规用大写环境变量名（如 MYSQL_HOST）"
                         % key, code="invalid_argument")

    skill_dir, _ = _resolve(a)
    path = os.path.join(skill_dir, CONFIG_NAME)
    data = _read_json(path)
    before = dict(data)
    for key, value in config.items():
        data[key] = _norm(value)
    content = _write_json(path, data)

    example = _read_json(os.path.join(skill_dir, EXAMPLE_NAME))
    items = [{"key": k, "complex": isinstance(data[k], (dict, list))} for k in config]
    return ok({
        "skill": os.path.basename(skill_dir),
        "file": path,
        "written": {k: data[k] for k in config},
        "added": [k for k in config if k not in before],
        "updated": [k for k in config if k in before and before[k] != data[k]],
        "kept": [k for k in before if k not in config],  # 未传的键原样保留，只改了传入的
        "missing_example": [k for k in config if k not in example],
        "content": content,
        "hints": _hints(skill_dir, items),
    })


def _action_remove(a):
    keys = a.get("keys")
    if not isinstance(keys, list) or not keys:
        return error("keys 必须是非空数组", code="invalid_argument")
    for key in keys:
        if not isinstance(key, str) or not KEY_RE.match(key):
            return error("非法配置键 %r" % key, code="invalid_argument")

    skill_dir, _ = _resolve(a)
    path = os.path.join(skill_dir, CONFIG_NAME)
    data = _read_json(path)
    removed = [k for k in keys if k in data]
    if removed:
        for key in removed:
            data.pop(key, None)
        content = _write_json(path, data)
    else:
        content = ""
    return ok({
        "skill": os.path.basename(skill_dir),
        "file": path,
        "removed": removed,
        "content": content,
    })


ACTIONS = {"keys": _keys_report, "set": _action_set, "remove": _action_remove}


def run(arguments):
    a = arguments if isinstance(arguments, dict) else {}
    # 没写 action：给了 config 就写回，否则列待配键
    action = str(a.get("action") or ("set" if a.get("config") else "keys")).strip().lower()
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
