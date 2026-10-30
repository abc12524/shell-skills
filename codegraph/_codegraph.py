"""codegraph 调用层：定位 CLI、执行子命令、把 JSON 输出整理成给 LLM 的形状。

只依赖标准库；CLI 由 npm 包 codegraph 提供（Windows 上是 codegraph.cmd）。
"""
import json
import os
import re
import shutil
import subprocess

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
_WIN = os.name == "nt"

# 默认超时：查询类很短，索引类可能跑几分钟
DEFAULT_TIMEOUT = 120
INDEX_TIMEOUT = 600


class CodegraphError(Exception):
    def __init__(self, message, code="codegraph_error"):
        super().__init__(message)
        self.code = code


def load_config():
    """读同目录 config.json（键为环境变量名），只取标量。"""
    path = os.path.join(_SKILL_DIR, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(v, (str, int, float)) and not isinstance(v, bool)
    }


_config = None


def _cfg():
    global _config
    if _config is None:
        _config = load_config()
        for key, value in _config.items():
            os.environ.setdefault(key, str(value))
    return _config


def _candidates():
    """按优先级列出可能的 CLI 路径（先显式配置，再 PATH，最后常见安装位置）。"""
    env = (os.environ.get("CODEGRAPH_BIN") or "").strip()
    if env:
        yield env

    found = shutil.which("codegraph")
    if found:
        yield found

    names = ["codegraph.cmd", "codegraph.exe", "codegraph"] if _WIN else ["codegraph"]
    roots = []
    if _WIN:
        local = os.environ.get("LOCALAPPDATA")
        roaming = os.environ.get("APPDATA")
        if local:
            roots.append(os.path.join(local, "codegraph", "current", "bin"))
            roots.append(os.path.join(local, "Programs", "codegraph", "bin"))
        if roaming:
            roots.append(os.path.join(roaming, "npm"))
        pf = os.environ.get("ProgramFiles")
        if pf:
            roots.append(os.path.join(pf, "nodejs"))
    else:
        roots += ["/usr/local/bin", "/usr/bin", "/opt/homebrew/bin",
                  os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.npm-global/bin")]
    for root in roots:
        for name in names:
            yield os.path.join(root, name)


_cli = None


def find_cli():
    """返回可执行的 codegraph 路径；找不到返回 None。"""
    global _cli
    if _cli:
        return _cli
    _cfg()
    for path in _candidates():
        if not path:
            continue
        if os.path.isfile(path) or shutil.which(path):
            _cli = path
            return _cli
    return None


def _decode(data):
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            text = data.decode(enc).strip()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = data.decode(locale_enc(), errors="replace").strip()
    # CLI 的进度/提示带颜色，模型读到 \x1b[34m 这类转义没意义
    return _ANSI.sub("", text)


def locale_enc():
    import locale
    return locale.getpreferredencoding(False) or "utf-8"


def run_cli(args, timeout=DEFAULT_TIMEOUT):
    """执行 codegraph 子命令，返回 (returncode, stdout, stderr)。"""
    binary = find_cli()
    if not binary:
        raise CodegraphError(
            "未找到 codegraph 命令：请先 npm i -g codegraph，"
            "或在技能目录 config.json 里把 CODEGRAPH_BIN 指向可执行文件",
            code="cli_missing",
        )
    cmd = [binary] + [str(x) for x in args]
    kwargs = dict(capture_output=True, timeout=timeout)
    try:
        proc = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        raise CodegraphError("codegraph 执行超时（%ss），可加大 timeout 参数" % timeout, code="timeout")
    except FileNotFoundError:
        raise CodegraphError("codegraph 路径不可执行：%s" % binary, code="cli_missing")
    except OSError:
        if not _WIN:
            raise
        # 个别 Windows 环境不能直接 CreateProcess 一个 .cmd，回退到 cmd /c
        try:
            proc = subprocess.run(["cmd", "/c"] + cmd, **kwargs)
        except subprocess.TimeoutExpired:
            raise CodegraphError("codegraph 执行超时（%ss），可加大 timeout 参数" % timeout, code="timeout")
    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def parse_json(text):
    """CLI 的 JSON 输出解析；失败返回 None（调用方退化为返回原文）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    # 少数情况前面会混入日志行：从第一个 [ / { 起再试一次
    for idx, ch in enumerate(text):
        if ch in "[{":
            try:
                return json.loads(text[idx:])
            except ValueError:
                return None
    return None


def project_path(value=None):
    """项目根目录：参数 > CODEGRAPH_PROJECT > 当前工作目录。"""
    _cfg()
    raw = value or os.environ.get("CODEGRAPH_PROJECT") or os.getcwd()
    return os.path.abspath(os.path.expanduser(str(raw)))


def run_json(args, timeout=DEFAULT_TIMEOUT):
    code, out, err = run_cli(args, timeout=timeout)
    if code != 0:
        detail = (err or out or "").strip()
        raise CodegraphError(
            "codegraph %s 失败（exit %s）：%s" % (args[0], code, detail[:400] or "无输出"),
            code="cli_failed",
        )
    data = parse_json(out)
    if data is None:
        raise CodegraphError(
            "codegraph %s 的输出不是 JSON：%s" % (args[0], (out or err)[:200]),
            code="bad_output",
        )
    return data


def status(path, timeout=DEFAULT_TIMEOUT):
    """读取索引状态；未初始化时 CLI 返回 {"initialized": false}（exit 0）。"""
    data = run_json(["status", "-j", path], timeout=timeout)
    return data if isinstance(data, dict) else {"initialized": False}


def ensure_indexed(path, timeout=DEFAULT_TIMEOUT):
    """确认项目已建索引；否则抛出带操作指引的错误。"""
    st = status(path, timeout=timeout)
    if not st.get("initialized"):
        raise CodegraphError(
            "项目未建立 codegraph 索引：%s（先调用本技能 action=index 建索引，"
            "或在项目里执行 codegraph init）" % path,
            code="not_indexed",
        )
    return st


def excerpt(project, rel_path, start, end, max_lines=40, max_chars=4000):
    """读源码片段给模型看；失败返回空串（不影响主结果）。"""
    try:
        start = int(start or 1)
        end = int(end or start)
    except (TypeError, ValueError):
        return ""
    if end < start:
        end = start
    end = min(end, start + max_lines - 1)
    full = os.path.join(project, str(rel_path).replace("\\", os.sep).replace("/", os.sep))
    text = None
    for enc in ("utf-8", "gbk"):
        try:
            with open(full, "r", encoding=enc) as fh:
                text = fh.read()
            break
        except (OSError, UnicodeDecodeError):
            continue
    if text is None:
        return ""
    lines = text.splitlines()
    if start > len(lines):
        return ""
    chunk = "\n".join(lines[start - 1:end])
    if len(chunk) > max_chars:
        chunk = chunk[:max_chars] + "\n... (截断)"
    return chunk


def trim_node(node, project=None, with_code=False, max_lines=40):
    """把 codegraph 的 node 对象压成 {name, kind, file, line, end_line} 这类小字段。"""
    if not isinstance(node, dict):
        return {}
    item = {
        "name": node.get("name") or node.get("qualifiedName") or "",
        "kind": node.get("kind") or "",
        "file": node.get("filePath") or "",
    }
    start, end = node.get("startLine"), node.get("endLine")
    if isinstance(start, int):
        item["line"] = start
        if isinstance(end, int) and end != start:
            item["end_line"] = end
    qualified = node.get("qualifiedName")
    if qualified and qualified != item["name"]:
        item["qualified"] = qualified
    if with_code and project and item["file"] and isinstance(start, int):
        code = excerpt(project, item["file"], start, end, max_lines=max_lines)
        if code:
            item["code"] = code
    return item


_PROGRESS = re.compile(r"^[\s|.*\-=#%\d]+$")


def tidy(text):
    """去掉 CLI 进度条残留（\\r 覆写、#####、纯符号行），并合并相邻重复行。"""
    text = (text or "").replace("\r", "\n")
    lines = []
    for raw in text.split("\n"):
        line = raw.strip().lstrip("|").strip().lstrip(".").strip()
        if not line or _PROGRESS.match(line) or "####" in line:
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return "\n".join(lines)


def tail(text, limit=300):
    """清理后截取末尾 limit 字符：进度输出的结论通常在最后。"""
    text = tidy(text)
    return text if len(text) <= limit else "..." + text[-limit:]
