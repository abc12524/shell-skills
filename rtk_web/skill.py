# skill: rtk_web
# description: rtk-web 文本压缩（HTTP 服务）：执行命令 / 压缩文本 / 上传文件，返回精简输出与节省统计
# status: enable
# usage:
#   arguments.action = run（默认，本地执行 cmd 并压缩 stdout+stderr）| text | file | filters | health
#   arguments.cmd | text | path = 对应输入：命令串 / 待压缩文本 / 文本文件路径
#   arguments.filter = auto（默认，按命令名自动选）| 过滤器名（见 action=filters）| none
#   arguments.mode = auto（默认，激进有损，省得多）| chain（固定链，可预期）
#   arguments.format = term（默认；action=file 时默认按文件名推断）| json | web | matches | lockfile | auto
#   arguments.lossless = true 只做 ansi+dedup；另支持 shell(bash/powershell/cmd/none)、cwd、timeout、charset、host
#   配置: RTK_WEB_HOST（默认 http://127.0.0.1:8787）；仅标准库
#   返回: result.text = 压缩后文本；bytes/tokens/saved_percent = 统计；run 另有 exit_code、compression
#   例: {"action":"run","cmd":"ls -l"} / {"action":"run","cmd":"cargo test","cwd":"D:/repo"}
#   例: {"action":"text","text":"..."} / {"action":"file","path":"build.log"} / {"action":"filters","q":"cargo"}
"""rtk_web —— 把命令输出 / 文本交给局域网 rtk-web 服务压缩，返回 LLM 友好的精简文本。

服务端点：
  POST {host}/api/compress?format=&mode=&charset=&lossless=&filter=   body = 原始文本
  POST {host}/api/compress/file                                     multipart，字段名 file
  GET  {host}/api/filters | /api/formats | /health

设计要点 / 踩坑记录：
  * 结果只给一份文本（result.text）+ 一份统计，不重复渲染同一内容。
  * mode=auto 激进（600 行 npm 日志实测省 95.5%，中段插入 "[… N lines elided …]" 标记，
    不会静默丢内容）；mode=chain 是固定链 ansi>progress>dedup>base64-elide>cap（约省 58%），
    需要可预期压缩时用它。
  * filter 优先于 format / mode / lossless；命令输出格式不匹配时服务**静默回退原文**
    （x-rtk-filter 仍是 filter:xxx，但节省率为 0），所以只在格式确定时才自动套 filter。
  * 服务端 ls 过滤器对中文 locale 日期（"4月 20  2026"）解析失败 → 自动套 ls/ls-short 时强制 LC_ALL=C。
  * 需要特定输出格式的过滤器（git-status 要 --porcelain、go-test 要 -json、eslint 要 -f json、
    pip-list 要 --format=json、kubectl 要 -o json 等）仅在命令带对应参数时才自动启用，见 _resolve_filter。
  * 命令带管道 / 重定向时不做自动 filter 推断（过滤器看到的是拼接后的混合输出）。
  * 服务不可达时**回退原文**（compression=unavailable + reason），不让压缩成为单点故障。
  * 请求体上限 10 MiB（服务端 413），本地超过 9 MiB 时首尾各半截断并插入提示行。
  * 本 skill 跑在 App 内置 Python 里，path / cwd 要写 Windows 路径（如 D:/repo、
    C:/Users/x/AppData/Local/Temp/build.log）；Unix 风格 /tmp 不存在，会报 not_found。
  * 服务端 4xx（未知 filter、二进制文件、超限）直接返回 error 让调用方自纠；
    只有连不上（超时/拒绝）才回退原文（compression=unavailable）。
  * 非零退出码不算失败：照常压缩并返回 exit_code，交调用方判断。
  * run 默认用 `bash -lc`，会读宿主 profile；子进程 env 已补 HOME / SHELL / PATH 默认值，
    避免远端缺这两个变量时 .bashrc 的 dircolors、$HOME/.cargo/env 报错混进每次输出。
  * shell=none 不做 shell 解析，继承当前 env，但不读 profile、不能用内建命令 / 管道；
    PATH 缺失时会补一份默认 PATH，不会凭空丢掉已有环境变量。
  * 服务只做压缩、不执行命令；run 动作是在本地 subprocess 里执行命令。
  * 配置优先级：host 参数 > 环境变量 RTK_WEB_HOST > 同目录 config.json（键 RTK_WEB_HOST，兼容旧写法 host）> 内置默认地址。
"""
import json
import os
import shlex
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

DEFAULT_HOST = "http://127.0.0.1:8787"
SERVICE = "rtk-web"
TIMEOUT = 45
MAX_INPUT = 9 * 1024 * 1024
FORMATS = ("term", "json", "web", "matches", "lockfile", "auto")
MODES = ("chain", "auto")
CHARSETS = ("auto", "utf-8", "utf-16le", "utf-16be", "gbk")
SHELLS = ("bash", "powershell", "cmd", "none")
ACTIONS = ("run", "text", "file", "filters", "health")
TRUTHY = (True, 1, "1", "true", "True", "yes", "on")
# 已知子命令词：用于跳过 -C / --git-dir / --schema 这类带值选项的值
SUB_WORDS = (
    "test", "tests", "nextest", "build", "clippy", "install", "check", "compile", "package",
    "dependencies", "connectedAndroidTest", "status", "diff", "branch", "log", "stash",
    "generate", "migrate", "db", "get", "describe", "list", "outdated", "i", "ci", "add",
    "run", "format", "push", "pods", "services", "vet",
)


def _load_config():
    """配置优先级：环境变量 > 同目录 config.json > 内置默认值。"""
    host = DEFAULT_HOST
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError) as exc:
            raise RuntimeError("读取 config.json 失败：%s" % exc)
        if isinstance(data, dict):
            for key in ("RTK_WEB_HOST", "host"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    host = val.strip()
    env = os.environ.get("RTK_WEB_HOST")
    if env and env.strip():
        host = env.strip()
    return host


def _host(a):
    h = str(a.get("host") or _load_config()).strip().rstrip("/")
    if not h.startswith(("http://", "https://")):
        h = "http://" + h
    return h


def _num(v, default):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _url(host, path, params):
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    return host + path + (("?" + q) if q else "")


def _http(url, data=None, ctype="application/octet-stream", timeout=TIMEOUT):
    """返回 (body, headers_lower, err)；err 非空表示失败。"""
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), {k.lower(): v for k, v in r.headers.items()}, None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace").strip()
        try:
            raw = json.loads(raw).get("error") or raw
        except ValueError:
            pass
        return None, {}, "HTTP %s: %s" % (e.code, raw)
    except (urllib.error.URLError, OSError) as e:
        return None, {}, "无法连接服务: %s" % e


def _failure_code(err):
    """HTTP 4xx 属于参数/输入问题，其余按 IO 错误处理。"""
    return "invalid_argument" if str(err).startswith("HTTP 4") else "io_error"


def _clip(data):
    """超过本地上限时保留首尾，中间插入提示。"""
    if len(data) <= MAX_INPUT:
        return data
    half = MAX_INPUT // 2
    note = "\n[… 本地截断：原始 %d 字节，仅发送首尾各 %d 字节 …]\n" % (len(data), half)
    return data[:half] + note.encode("utf-8") + data[-half:]


def _clip_file(path):
    half = MAX_INPUT // 2
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(half)
        f.seek(-half, os.SEEK_END)
        tail = f.read(half)
    note = "\n[… 本地截断：原始 %d 字节，仅发送首尾各 %d 字节 …]\n" % (size, half)
    return head + note.encode("utf-8") + tail


def _multipart(name, data):
    boundary = "----rtkweb%s" % os.urandom(8).hex()
    safe = os.path.basename(name or "upload.txt").replace('"', "_").replace("\r", "_").replace("\n", "_")
    head = (
        "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n" % (boundary, safe)
    ).encode("utf-8")
    body = head + data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return body, "multipart/form-data; boundary=%s" % boundary


def _params(a, filter_name, format_default="term"):
    mode = str(a.get("mode") or "auto").strip().lower()
    if mode not in MODES:
        return None, "mode 只能是 " + " / ".join(MODES)
    raw_fmt = a.get("format")
    if raw_fmt:
        fmt = str(raw_fmt).strip().lower()
    else:
        fmt = format_default
    if fmt is not None and fmt not in FORMATS:
        return None, "format 只能是 " + " / ".join(FORMATS)
    cs = str(a.get("charset") or "auto").strip().lower()
    if cs not in CHARSETS:
        return None, "charset 只能是 " + " / ".join(CHARSETS)
    p = {"mode": mode, "charset": cs}
    if fmt is not None:
        p["format"] = fmt
    if a.get("lossless") in TRUTHY:
        p["lossless"] = "true"
    if filter_name and filter_name != "none":
        p["filter"] = filter_name
    return p, None


def _decorate(payload, headers):
    def stat(name, cast):
        try:
            return cast(headers["x-rtk-" + name])
        except (KeyError, TypeError, ValueError):
            return cast(0)

    return {
        "text": payload.decode("utf-8", "replace"),
        "filter": headers.get("x-rtk-filter") or "-",
        "chain": headers.get("x-rtk-chain") or "",
        "saved_percent": stat("saved-percent", float),
        "bytes": {"before": stat("bytes-before", int), "after": stat("bytes-after", int)},
        "tokens": {"before": stat("tokens-before", int), "after": stat("tokens-after", int)},
    }


def _first_sub(args, candidates=None):
    """取子命令；给 candidates 时跳过 -C / --git-dir 这类带值选项的值。"""
    for t in args:
        if t.startswith("-"):
            continue
        if candidates is None or t in candidates:
            return t
    return ""


def _has_flag(args, *flags):
    for a in args:
        for f in flags:
            if a == f or a.startswith(f + "="):
                return True
    return False


def _flag_value(args, *flags):
    """取选项值：-f json / --format=json / -ojson / --output json。"""
    for i, a in enumerate(args):
        for f in flags:
            if a == f and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(f + "="):
                return a[len(f) + 1:]
            if len(f) == 2 and a.startswith(f) and len(a) > 2 and not a.startswith("--"):
                return a[2:]
    return ""


def _is_json(value):
    return value.strip().lower() == "json"


def _format_is_json(args):
    return _is_json(_flag_value(args, "-f", "--format"))


def _output_is_json(args):
    return _is_json(_flag_value(args, "-o", "--output"))


def _resolve_filter(cmd):
    """按命令名推断命令专属过滤器；不确定（管道/重定向/格式不匹配）时返回 None。"""
    if not cmd or any(ch in cmd for ch in "|><"):
        return None
    toks = cmd.split()
    i = 0
    while i < len(toks) and ("=" in toks[i] and not toks[i].startswith("-")):
        i += 1  # 跳过 VAR=value 前缀
    while i < len(toks) and toks[i] in ("sudo", "time", "nice", "nohup", "env", "command"):
        i += 1
    rest = toks[i:]
    if not rest:
        return None
    prog = os.path.basename(rest[0]).lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if prog.endswith(suffix):
            prog = prog[: -len(suffix)]
            break
    args = rest[1:]
    sub = _first_sub(args, SUB_WORDS)

    if prog == "ls":
        long_fmt = any(a.startswith("-") and not a.startswith("--") and "l" in a[1:] for a in args)
        return "ls" if (long_fmt or _has_flag(args, "--long")) else "ls-short"
    if prog in ("tree", "wc", "mypy", "tsc", "black", "prettier", "phpunit", "pest", "pint"):
        return prog
    if prog == "cargo":
        return {
            "test": "cargo-test",
            "nextest": "cargo-nextest",
            "build": "cargo-build",
            "clippy": "cargo-clippy",
            "install": "cargo-install",
        }.get(sub)
    if prog == "go" and sub in ("test", "build", "vet"):
        if sub == "test" and not _has_flag(args, "-json"):
            return None  # go-test 过滤器要求 -json
        return "go-" + sub
    if prog == "pytest" or (prog.startswith("python") and "pytest" in args):
        return "pytest"
    if prog == "ruff":
        return {"check": "ruff-check", "format": "ruff-format"}.get(sub)
    if prog == "eslint":
        return "eslint" if _format_is_json(args) else "lint"
    if prog == "rubocop":
        return "rubocop" if _format_is_json(args) else "rubocop-text"
    if prog == "rspec":
        return "rspec" if _format_is_json(args) else "rspec-text"
    if prog in ("mvn", "mvnw"):
        return {"test": "mvn-test", "compile": "mvn-compile", "package": "mvn-package"}.get(sub)
    if prog in ("gradle", "gradlew"):
        if sub == "dependencies":
            return "gradle-dependencies"
        return {"test": "gradle-test", "connectedAndroidTest": "gradle-connected"}.get(sub)
    if prog in ("npm", "pnpm", "next", "ng"):
        if prog == "pnpm" and sub in ("install", "i", "add"):
            return "pnpm-install"
        if prog in ("next", "ng"):
            return prog
        return "npm" if sub in ("install", "i", "ci", "add") else None
    if prog in ("pip", "pip3"):
        if sub in ("list", "outdated") and _is_json(_flag_value(args, "--format")):
            if sub == "outdated" or _has_flag(args, "--outdated"):
                return "pip-outdated"
            return "pip-list"
        return None
    if prog == "prisma":
        if sub == "generate":
            return "prisma-generate"
        if sub == "migrate" and len(args) > 1 and args[1] in ("dev", "deploy", "status"):
            return "prisma-migrate-" + args[1]
        if sub == "db" and len(args) > 1 and args[1] == "push":
            return "prisma-db-push"
        return None
    if prog == "kubectl":
        if sub == "get" and _output_is_json(args):
            if "pods" in args or "pod" in args:
                return "kubectl-pods"
            if "services" in args or "svc" in args:
                return "kubectl-services"
        return None
    if prog == "git":
        if sub == "status" and any(a.startswith("--porcelain") for a in args):
            return "git-status"
        if sub == "diff":
            return "git-diff"
        if sub == "branch":
            return "git-branch"
        if sub == "stash" and "list" in args:
            return "git-stash-list"
        return None
    return None


def _filter_arg(a, cmd):
    raw = a.get("filter")
    if raw is None:
        return _resolve_filter(cmd) if cmd else None
    raw = str(raw).strip()
    if not raw or raw.lower() == "none":
        return None
    if raw.lower() == "auto":
        return _resolve_filter(cmd) if cmd else None
    return raw.lower()


def _shell_argv(cmd, shell):
    if shell == "none":
        argv = shlex.split(cmd, posix=(os.name != "nt"))
        if os.name == "nt":
            # posix=False 会保留包裹引号，这里去掉成对引号
            argv = [
                t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'"
                else t
                for t in argv
            ]
        return argv, None
    if shell == "bash":
        exe = None
        if os.name == "nt":
            for cand in (
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files (x86)\Git\bin\bash.exe",
                r"C:\Program Files\Git\usr\bin\bash.exe",
            ):
                if os.path.isfile(cand):
                    exe = cand
                    break
        exe = exe or shutil.which("bash") or shutil.which("bash.exe")
        if not exe and os.name != "nt":
            # 精简 Linux / Android 没有 bash 时退回 sh
            exe = shutil.which("sh")
            if exe:
                return [exe, "-c", cmd], None
        if not exe:
            return None, "找不到 bash；Windows 上可改用 shell=cmd 或 shell=powershell"
        return [exe, "-lc", cmd], None
    if shell == "powershell":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            return None, "找不到 powershell / pwsh"
        return [exe, "-NoProfile", "-Command", cmd], None
    if shell == "cmd":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", cmd], None
    return None, "shell 只能是 " + " / ".join(SHELLS)


def _child_env(argv0, shell):
    """补 HOME / SHELL / PATH，避免登录 shell 因缺变量触发 profile 报错。"""
    env = dict(os.environ)
    if not env.get("HOME"):
        home = os.path.expanduser("~")
        if home and home != "~":
            env["HOME"] = home
    if os.name != "nt" and not env.get("SHELL"):
        env["SHELL"] = argv0 if shell == "bash" else "/bin/sh"
    if os.name != "nt" and not env.get("PATH"):
        home = env.get("HOME")
        parts = []
        if home:
            parts.extend((
                os.path.join(home, ".cargo", "bin"),
                os.path.join(home, ".local", "bin"),
            ))
        parts.extend((
            "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
        ))
        env["PATH"] = os.pathsep.join(parts)
    return env


def _compress_bytes(host, data, a, timeout, filter_name=None):
    """POST /api/compress，返回 (dict, err)。"""
    params, perr = _params(a, filter_name if filter_name is not None else _filter_arg(a, None))
    if perr:
        return None, perr
    clipped = len(data) > MAX_INPUT
    if clipped:
        data = _clip(data)
    payload, headers, err = _http(_url(host, "/api/compress", params), data, timeout=timeout)
    if payload is None:
        return None, err
    res = _decorate(payload, headers)
    if clipped:
        res["note"] = "输入超过 9 MiB，已本地首尾截断"
    return res, None


def run(arguments):
    a = arguments if isinstance(arguments, dict) else {}
    action = str(a.get("action") or "run").strip().lower()
    if action in ("help", "usage", "list"):
        return ok({
            "service": SERVICE,
            "actions": list(ACTIONS),
            "params": ["cmd", "text", "path", "filter", "mode", "format", "lossless",
                       "shell", "cwd", "timeout", "charset", "host"],
            "note": "filter 取值见 action=filters；mode=auto 省得多，chain 可预期",
        })
    if action not in ACTIONS:
        return error("未知 action: %s（可选 %s）" % (action, " / ".join(ACTIONS)), code="invalid_argument")

    try:
        host = _host(a)
    except RuntimeError as exc:
        return error(str(exc), code="needs_config")
    timeout = _num(a.get("timeout"), TIMEOUT)

    if action == "health":
        raw, _h, err = _http(host + "/health", timeout=timeout)
        if raw is None:
            return error("%s 不可用（%s）" % (host, err), code="io_error")
        try:
            info = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            info = {"raw": raw.decode("utf-8", "replace")[:200]}
        if isinstance(info, dict):
            info["host"] = host
        return ok(info)

    if action == "filters":
        raw, _h, err = _http(host + "/api/filters", timeout=timeout)
        if raw is None:
            return error("%s 不可用（%s）" % (host, err), code="io_error")
        try:
            names = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return error("过滤器列表解析失败", code="internal")
        q = str(a.get("q") or "").strip().lower()
        if q:
            names = [n for n in names if q in str(n).lower()]
        return ok({"host": host, "count": len(names), "filters": names})

    if action == "text":
        text = a.get("text")
        if not isinstance(text, str) or not text:
            return error("action=text 需要非空 arguments.text", code="invalid_argument")
        res, err = _compress_bytes(host, text.encode("utf-8"), a, timeout)
        if res is None:
            return error("压缩失败：%s" % err, code=_failure_code(err))
        return ok(res)

    if action == "file":
        path = a.get("path")
        if not isinstance(path, str) or not path:
            return error("action=file 需要 arguments.path", code="invalid_argument")
        if not os.path.isfile(path):
            return error("文件不存在: %s" % path, code="not_found")
        filter_name = _filter_arg(a, None)
        params, perr = _params(a, filter_name, format_default=None)
        if perr:
            return error(perr, code="invalid_argument")
        clipped = os.path.getsize(path) > MAX_INPUT
        if clipped:
            data = _clip_file(path)
        else:
            with open(path, "rb") as f:
                data = f.read()
        body, ctype = _multipart(path, data)
        payload, headers, err = _http(_url(host, "/api/compress/file", params), body, ctype, timeout)
        if payload is None:
            return error("压缩失败：%s" % err, code=_failure_code(err))
        res = _decorate(payload, headers)
        res["file"] = path
        if clipped:
            res["note"] = "文件超过 9 MiB，已本地首尾截断"
        return ok(res)

    # action == "run"
    cmd = a.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return error("action=run 需要 arguments.cmd", code="invalid_argument")
    cmd = cmd.strip()
    shell = str(a.get("shell") or "bash").strip().lower()
    cwd = a.get("cwd") or None
    if cwd is not None and not (isinstance(cwd, str) and os.path.isdir(cwd)):
        return error("cwd 不存在: %s" % cwd, code="not_found")
    argv, aerr = _shell_argv(cmd, shell)
    if argv is None:
        return error(aerr, code="invalid_argument")
    filter_name = _filter_arg(a, cmd)
    params, perr = _params(a, filter_name)
    if perr:
        return error(perr, code="invalid_argument")

    env = _child_env(argv[0], shell)
    if filter_name in ("ls", "ls-short"):
        env["LC_ALL"] = "C"
        env.pop("LANG", None)
        env.pop("LANGUAGE", None)
    try:
        cp = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return error("命令超时（>%gs）：%s" % (timeout, cmd), code="internal")
    except OSError as ex:
        return error("命令执行失败：%s" % ex, code="io_error")

    data = cp.stdout or b""
    clipped = len(data) > MAX_INPUT
    if clipped:
        data = _clip(data)
    payload, headers, err = _http(_url(host, "/api/compress", params), data,
                                  timeout=max(timeout, 30))
    base = {"cmd": cmd, "exit_code": cp.returncode}
    if payload is None:
        if err.startswith("HTTP 4"):  # 参数/过滤器错误：报错让调用方自纠，不伪成功
            return error("服务拒绝（%s）：%s" % (cmd, err), code="invalid_argument")
        base.update({
            "compression": "unavailable",
            "reason": err,
            "text": data.decode("utf-8", "replace"),
            "saved_percent": 0.0,
            "bytes": {"before": len(data), "after": len(data)},
        })
        return ok(base)
    base.update(_decorate(payload, headers))
    base["compression"] = "ok"
    if clipped:
        base["note"] = "输出超过 9 MiB，已本地首尾截断"
    return ok(base)
