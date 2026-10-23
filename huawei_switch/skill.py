# skill: huawei_switch
# description: 华为 VRP 设备（路由器 / 交换机，旧版 SSH）常驻会话客户端：跟踪/切换视图、查 port 状态；Y/N 须显式应答；仅 Linux
# status: enable
# usage:
#   arguments.action  = run（默认）| send | start | stop | status | state | up | top | answer | next
#   arguments.command = 命令文本（run / send / answer 必填）
#   arguments.prefix  = 仅 next，原样拼 "?"："display " 查子命令、"disp" 查补全
#   arguments.page    = 仅 next：页码（默认 1），每页 10 条
#   arguments.limit   = 仅 next：每页条数（默认 10，最大 50）
#   arguments.timeout = 单条命令超时秒数（默认 25）
#   arguments.keep    = true（默认，保持会话）| false（执行完结束会话）
#   run/send 执行命令（run 自动建会话、断线自动重连一次）；start/stop/status 管理会话
#   state 查当前视图 / 是否未保存 / 是否挂着 Y/N；up 上一层；top 回用户视图
#   answer 显式应答 Y/N（command="y"|"n"）；挂着 Y/N 时 run/send/up/top 会被拒绝
#   next 向设备要当前视图的 ? 帮助，分页返回 next_steps（每页 10 条，page 翻页）
#   返回: text、view/view_target/prompt/unsaved_changes、needs_confirm/confirm_text；
#         next 额外返回 total/page/total_pages/next_steps（来自设备帮助，非本地写死）
#   示例: {"action":"run","command":"display interface brief"}
#         {"action":"run","command":"system-view"} → {"action":"run","command":"interface GigabitEthernet0/0/1"} → {"action":"up"} → {"action":"top"}
#         {"action":"run","command":"save"} → needs_confirm=true → {"action":"answer","command":"n"}
#   配置: HUAWEI_SWITCH_HOST / HUAWEI_SWITCH_USER / HUAWEI_SWITCH_PASS /
#         HUAWEI_SWITCH_NAME（设备提示符中的主机名）/ HUAWEI_SWITCH_HOME /
#         HUAWEI_SWITCH_KEEPALIVE（空闲保活秒数，默认 120，0=关闭）
#   依赖: 无（标准库 + 系统 ssh）；仅 Linux（Windows 调用会明确报错）
"""华为 VRP 设备常驻会话客户端 skill（shell-tool 运行时；需要 Linux pty）。"""
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time

try:
    import pty
except ImportError:  # Windows 等无 pty 的平台：保留模块可导入，调用时给出明确错误
    pty = None

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error


# ============= 配置 =============
def _load_config():
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


_load_config()

HOST = os.environ.get("HUAWEI_SWITCH_HOST", "192.168.1.1")
USER = os.environ.get("HUAWEI_SWITCH_USER", "admin")
NAME = os.environ.get("HUAWEI_SWITCH_NAME", "Huawei")
PASS = os.environ.get("HUAWEI_SWITCH_PASS", "")
HOME = os.path.expanduser(os.environ.get("HUAWEI_SWITCH_HOME") or "~/.huawei_switch")
SOCK = os.path.join(HOME, "ctl.sock")
PIDF = os.path.join(HOME, "daemon.pid")
LOGF = os.path.join(HOME, "session.log")

PROMPT_RE = re.compile(
    rf"(?P<prompt>(?:<{re.escape(NAME)}[^<>\r\n]*>|\[~?{re.escape(NAME)}[^\[\]\r\n]*\]))")
# 提示符总是独占一行：只按“整行”删除，避免误删输出正文里形如 <设备名> 的文本
PROMPT_LINE_RE = re.compile(
    rf"(?m)^[ \t]*(?:<{re.escape(NAME)}[^<>\r\n]*>|\[~?{re.escape(NAME)}[^\[\]\r\n]*\])[ \t]*$")
MORE_RE = re.compile(r"---- *More *----")
CONF_RE = re.compile(r"\[Y/N\]|\(y/n\)")
PW_RE = re.compile(r"(?i)password:\s*")
YESNO_RE = re.compile(r"(?i)\(yes/no")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# 接口类视图名（VRP 用视图后缀表示接口/逻辑接口）
_IFACE_RE = re.compile(
    r"^(GigabitEthernet|XGigabitEthernet|Ethernet|GE|XGE|Eth-Trunk|Vlanif|Vlan|"
    r"LoopBack|Tunnel|Serial|Cellular|Virtual-Template|Wlan|NULL|MEth|Vbdif|Nve)")

def _parse_prompt(prompt):
    """把提示符文本解析成视图信息。"""
    raw = (prompt or "").strip()
    dirty = "~" in raw
    body = raw.strip("<>[]").lstrip("~")
    if raw.startswith("<"):
        view, target = "user", ""
    else:
        rest = body[len(NAME):] if body.startswith(NAME) else body
        rest = rest.lstrip("-")
        if not rest:
            view, target = "system", ""
        elif _IFACE_RE.match(rest):
            view, target = "interface", rest
        else:
            view, target = "other", rest
    return {
        "prompt": raw,
        "view": view,
        "target": target,
        "dirty": dirty,
    }


def _ssh_argv():
    return [
        "ssh", "-tt",
        "-oKexAlgorithms=+diffie-hellman-group14-sha1",
        "-oPubkeyAuthentication=no",
        "-oPreferredAuthentications=password",
        "-oNumberOfPasswordPrompts=1",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-oConnectTimeout=10",
        "-oServerAliveInterval=30",
        "-oServerAliveCountMax=3",
        "-oTCPKeepAlive=yes",
        "-oLogLevel=ERROR",
        f"{USER}@{HOST}",
    ]


def _clean(text):
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "")
    return text


def _strip(command, text):
    """去掉设备回显的命令行与独占一行的提示符。"""
    lines = text.split("\n")
    if lines and command.strip() and command.strip() in lines[0]:
        lines = lines[1:]
    return PROMPT_LINE_RE.sub("", "\n".join(lines)).strip("\n")


def _as_bool(value, default=True):
    """宽松解析布尔参数（JSON 布尔 / "false" / 0 等）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def _fail(message, code=None, extra=None):
    """构造错误信封；不支持额外字段的运行时（移动端）把状态降级进消息。"""
    if extra:
        try:
            return error(message, code=code, data=extra)
        except TypeError:
            detail = ", ".join(f"{k}={v}" for k, v in extra.items()
                               if v not in (None, "", False))
            if detail:
                message = f"{message}（{detail}）"
    return error(message, code=code)


# `?` 在线帮助的命令行：缩进 2~8 空格 + 命令名 + 2 空格以上 + 说明
HELP_CMD_RE = re.compile(r"^ {2,8}(\S+)\s{2,}(\S.*?)\s*$")
HELP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _parse_help(text):
    """把设备 `?` 的输出解析成 [{command, description}]（去重、保持顺序）。"""
    out, seen = [], set()
    for line in (text or "").split("\n"):
        m = HELP_CMD_RE.match(line)
        if not m:
            continue
        name, desc = m.group(1), m.group(2).strip()
        if not HELP_NAME_RE.match(name) or name in seen:
            continue
        seen.add(name)
        out.append({"command": name, "description": desc})
    return out


class _Session:
    """常驻 pty + ssh 会话，并跟踪当前所处视图。"""

    def __init__(self, password):
        if pty is None:
            raise RuntimeError("本 skill 需要 Linux pty（Windows 不支持），请在 Linux 主机运行")
        self.password = password
        self.pid, self.fd = pty.fork()
        if self.pid == 0:                       # child
            try:
                os.execvp("ssh", _ssh_argv())
            finally:
                os._exit(127)
        self.buf = ""
        self.alive = True
        self.pending = None                     # 设备正在等待的 Y/N 提示（None = 无）
        self.help_cache = {}                    # ? 帮助缓存：key=(命令, 视图提示符)
        self.last = time.time()                 # 最近一次与设备交互的时间
        self.state = {"prompt": "", "view": "unknown", "target": "", "dirty": False}
        os.makedirs(HOME, exist_ok=True)
        # 日志可能含设备配置（含密文口令），显式 0600，不受 umask 影响
        fd = os.open(LOGF, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.log = os.fdopen(fd, "ab", buffering=0)

    # ---- 视图状态 ----
    def _update_state(self, text):
        """取输出里最后一个提示符，更新当前视图。"""
        matches = list(PROMPT_RE.finditer(text or ""))
        if matches:
            self.state = _parse_prompt(matches[-1].group("prompt"))
        return self.state

    def state_snapshot(self):
        s = dict(self.state)
        s["alive"] = self.alive
        s["pending_confirm"] = bool(self.pending)
        s["confirm_text"] = (self.pending or {}).get("text", "")
        return s

    # ---- 低层读写 ----
    def _read(self, timeout):
        if not self.alive:
            return b""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        if not data:
            self.alive = False
            return b""
        try:
            self.log.write(data)
        except OSError:
            pass
        return data

    def _abort(self, quiet=0.6, limit=3.0):
        """给设备发 Ctrl+C 中止当前输出，再把残留输出抽干，避免污染下一条命令。"""
        try:
            self.send("\x03")
        except OSError:
            self.buf = ""
            return
        end = time.time() + limit
        while time.time() < end:
            data = self._read(quiet)
            if data is None or data == b"":
                break
        self.buf = ""

    def _wait(self, patterns, timeout):
        end = time.time() + timeout
        while True:
            for name, rx in patterns:
                m = rx.search(self.buf)
                if m:
                    out, self.buf = self.buf[:m.end()], self.buf[m.end():]
                    return name, out
            if not self.alive:
                out, self.buf = self.buf, ""
                return "eof", out
            remain = end - time.time()
            if remain <= 0:
                out, self.buf = self.buf, ""
                return "timeout", out
            data = self._read(min(0.5, remain))
            if data:
                self.buf += data.decode("utf-8", "replace")

    def idle_ping(self, timeout=3.0):
        """空闲保活：发一个空回车，刷新设备 VTY 的空闲计时（不产生副作用）。"""
        if not self.alive or self.pending:
            return False
        self._drain()
        self.buf = ""
        try:
            self.send("\r")
        except OSError:
            return False
        end = time.time() + timeout
        while time.time() < end:
            data = self._read(0.3)
            if data is None or data == b"":
                break
            self.buf += data.decode("utf-8", "replace")
        if not self.alive:
            self.buf = ""
            return False
        self._update_state(_clean(self.buf))
        self.buf = ""
        return True

    def _drain(self, quiet=0.2):
        """丢弃 pty 里残留的旧输出，避免串到下一命令。"""
        while True:
            data = self._read(quiet)
            if data is None or data == b"":
                return

    def send(self, text):
        os.write(self.fd, text.encode())

    def login(self):
        name, out = self._wait([("pw", PW_RE), ("yesno", YESNO_RE), ("prompt", PROMPT_RE)], 20)
        if name == "yesno":
            self.send("yes\r")
            name, out = self._wait([("pw", PW_RE), ("prompt", PROMPT_RE)], 20)
        if name != "pw":
            raise RuntimeError(f"未出现密码提示（{name}）：{_clean(out)[-160:]}")
        self.send(self.password + "\r")
        name, out = self._wait([("prompt", PROMPT_RE),
                                ("bad", re.compile(r"(?i)permission denied|authentication failed"))], 25)
        if name != "prompt":
            raise RuntimeError(f"登录失败（{name}）：{_clean(out)[-160:]}")
        self._update_state(out)
        self.send("screen-length 0 temporary\r")     # 关掉当前终端分页，不改配置
        _, out = self._wait([("prompt", PROMPT_RE), ("more", MORE_RE)], 10)
        self._update_state(out)

    def run(self, command, timeout=25.0, answer=False):
        """发一条命令并等提示符；answer=True 表示这是在回答挂起的 Y/N。"""
        if not self.alive:
            return {"ok": False, "eof": True, "error": "会话已断开，请重新 start",
                    "pending_confirm": self.pending, "state": self.state_snapshot()}
        if answer and not self.pending:
            return {"ok": False, "error": "当前没有待应答的 Y/N 提示（无需 answer）",
                    "state": self.state_snapshot()}
        if self.pending and not answer:
            # 设备仍停在 Y/N：这条命令会被当成应答，必须显式 answer
            return {"ok": False, "needs_confirm": True, "pending_confirm": self.pending,
                    "error": "设备正在等待 Y/N 确认：请先 action=answer（或 action=stop）",
                    "confirm_text": self.pending.get("text", ""),
                    "state": self.state_snapshot()}
        self._drain()
        self.buf = ""
        self.send(command + "\r")
        chunks = ""
        end = time.time() + timeout
        while True:
            remain = max(0.1, end - time.time())
            name, out = self._wait([("prompt", PROMPT_RE), ("more", MORE_RE),
                                    ("confirm", CONF_RE)], remain)
            chunks += out
            if name == "more":
                self.send(" ")
                continue
            text = _clean(chunks)
            if name != "confirm":
                self._update_state(text)
                self.pending = None
            else:
                self.pending = {"command": command,
                                "text": _strip(command, text)[-400:]}
            res = {"state": self.state_snapshot(), "command": command}
            if name == "confirm":
                res.update({"ok": True, "needs_confirm": True, "pending_confirm": self.pending,
                            "confirm_text": self.pending["text"],
                            "output": self.pending["text"]})
                return res
            if name == "timeout":
                self._abort()
                self.pending = None
                res.update({"ok": False,
                            "error": f"命令超时（{timeout}s，已发送 Ctrl+C 中止并清空残留输出）",
                            "pending_confirm": None, "output": _strip(command, text)})
                return res
            if name == "eof":
                self.pending = None
                res.update({"ok": False, "eof": True, "error": "连接被设备关闭",
                            "output": _strip(command, text)})
                return res
            res.update({"ok": True, "output": _strip(command, text), "needs_confirm": False,
                        "pending_confirm": None})
            return res

    def _clear_line(self, text="", timeout=5.0):
        """清掉留在输入行的半截命令（`display ?` 之后设备会保留半截输入）。

        `?` 只触发帮助、不消耗已输入内容；VRP 的 Ctrl+C 也不清行。用退格删除，
        多删几个（设备可能吞掉个别退格），再回车收尾；随后读到提示符才算干净。
        """
        n = min(len(text) + 8, 256)
        try:
            self.send("\x08" * n)
            self.send("\r")
        except OSError:
            pass
        buf = ""
        end = time.time() + timeout
        while time.time() < end:
            data = self._read(0.25)
            if data is None:
                if buf and PROMPT_RE.search(buf[-200:]):
                    break                       # 已回到提示符且输出安静
                continue
            if data == b"":
                break
            buf += data.decode("utf-8", "replace")
        self.buf = ""

    def query(self, text, timeout=12.0):
        """发送不回车的在线帮助（如 `?` / `display ?`），读到输出安静为止。"""
        if not self.alive:
            return {"ok": False, "eof": True, "error": "会话已断开，请重新 start",
                    "pending_confirm": self.pending, "state": self.state_snapshot()}
        if self.pending:
            return {"ok": False, "needs_confirm": True, "pending_confirm": self.pending,
                    "error": "设备正在等待 Y/N 确认：请先 action=answer（或 action=stop）",
                    "confirm_text": self.pending.get("text", ""),
                    "state": self.state_snapshot()}
        self._drain()
        self.buf = ""
        self.send(text)
        chunks = ""
        end = time.time() + timeout
        idle = 0
        while time.time() < end:
            data = self._read(0.35)
            if data is None:                    # 0.35s 无输出
                idle += 1
                if idle >= 2:                   # 累计 ~0.7s 安静，视为输出结束
                    break
                continue
            if data == b"":
                break
            idle = 0
            chunks += data.decode("utf-8", "replace")
        out = _clean(chunks)
        self._clear_line(text)                  # ? 帮助不消耗已输入内容，必须清行
        self._update_state(out)
        body = _strip(text, out)
        # 设备会在帮助后重显半截命令（如 "disp"），去掉尾部这行
        pending = text[:-1].strip()
        lines = body.rstrip().split("\n")
        if pending and lines and lines[-1].strip() == pending:
            body = "\n".join(lines[:-1]).rstrip()
        return {"ok": True, "output": body, "needs_confirm": False,
                "pending_confirm": None, "command": text,
                "state": self.state_snapshot()}

    def close(self):
        self.pending = None
        try:
            self.send("quit\r")
            time.sleep(0.4)
        except OSError:
            pass
        for fn in (lambda: os.close(self.fd),
                   lambda: os.kill(self.pid, signal.SIGTERM)):
            try:
                fn()
            except OSError:
                pass


# ============= 守护进程 =============
def _daemon(password):
    s = _Session(password)
    try:
        s.login()
    except Exception as e:                      # noqa: BLE001
        try:
            with open(os.path.join(HOME, "daemon.log"), "a", encoding="utf-8") as f:
                f.write(f"login failed: {e}\n")
        except OSError:
            pass
        os._exit(1)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(4)
    keepalive = float(os.environ.get("HUAWEI_SWITCH_KEEPALIVE") or 120)
    srv.settimeout(20 if keepalive <= 0 else max(1.0, min(20.0, keepalive)))
    pidfd = os.open(PIDF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(pidfd, "w") as f:
        f.write(str(os.getpid()))
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:                  # 空闲：按需发空回车保活，防 VTY 超时
            if not s.alive:
                os._exit(0)                     # 会话已死，退出让下次调用重建
            if keepalive > 0 and time.time() - s.last >= keepalive:
                s.idle_ping()
                s.last = time.time()
            continue
        with conn:
            try:
                raw = b""
                while not raw.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                req = json.loads(raw.decode() or "{}")
                act = req.get("action", "run")
                if act == "ping":
                    resp = {"ok": True, "pid": os.getpid(), "alive": s.alive,
                            "idle": round(time.time() - s.last, 1),
                            "keepalive": int(keepalive),
                            "state": s.state_snapshot()}
                elif act == "state":
                    resp = {"ok": True, "state": s.state_snapshot()}
                elif act == "close":
                    conn.sendall((json.dumps({"ok": True}) + "\n").encode())
                    s.close()
                    os._exit(0)
                elif act == "query":            # 不回车的在线帮助（`?`），按视图缓存
                    cmd = req.get("command", "?")
                    key = (cmd, s.state.get("prompt"))
                    if req.get("fresh") or key not in s.help_cache:
                        out = s.query(cmd, float(req.get("timeout", 12)))
                        if out.get("ok"):
                            s.help_cache[key] = out
                            if len(s.help_cache) > 64:
                                s.help_cache.clear()
                                s.help_cache[key] = out
                        resp = out
                    else:
                        resp = s.help_cache[key]
                else:
                    resp = s.run(req.get("command", ""), float(req.get("timeout", 25)),
                                 answer=bool(req.get("answer")))
                conn.sendall((json.dumps(resp) + "\n").encode())
                if act != "ping":               # 健康探测不算设备交互，别重置空闲计时
                    s.last = time.time()
            except Exception as e:              # noqa: BLE001
                try:
                    conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
                except OSError:
                    pass


def _daemon_main():
    """守护进程入口（由 _spawn 以独立会话启动）。"""
    _daemon(PASS)


def _rpc(payload, timeout: float = 120):
    cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cli.settimeout(timeout)
    cli.connect(SOCK)
    cli.sendall((json.dumps(payload) + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        chunk = cli.recv(65536)
        if not chunk:
            break
        data += chunk
    cli.close()
    return json.loads(data.decode() or "{}")


def _running():
    if not os.path.exists(SOCK):
        return False
    try:
        return bool(_rpc({"action": "ping"}, timeout=10).get("ok"))
    except (OSError, ValueError):
        return False


def _spawn():
    """以独立会话启动后台守护（不用双 fork，线程池里 fork 有死锁风险）。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    module = __name__ if __name__.startswith("skill.") else "skill.huawei_switch.skill"
    boot = (f"import importlib, sys; sys.path.insert(0, {root!r}); "
            f"importlib.import_module({module!r})._daemon_main()")
    logfd = os.open(os.path.join(HOME, "daemon.log"),
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen([sys.executable, "-c", boot],
                         stdin=subprocess.DEVNULL, stdout=logfd, stderr=logfd,
                         start_new_session=True, close_fds=True, cwd=root)
    except OSError as e:
        return False, f"启动守护进程失败 - {e}"
    finally:
        os.close(logfd)
    for _ in range(60):
        if _running():
            return True, "会话已就绪"
        time.sleep(0.25)
    return False, "启动超时（见 ~/.huawei_switch/daemon.log）"


def _start():
    if _running():
        return True, "会话已在运行"
    if pty is None:
        return False, "本 skill 需要 Linux pty（Windows 不支持），请在 Linux 主机运行"
    if not PASS or PASS == "your-password":
        return False, "未配置密码：请在本技能 config.json 设置 HUAWEI_SWITCH_PASS"
    os.makedirs(HOME, exist_ok=True)
    return _spawn()


def _stop():
    if not _running():
        for p in (SOCK, PIDF):
            try:
                os.unlink(p)
            except OSError:
                pass
        return True, "会话未运行"
    try:
        _rpc({"action": "close"}, timeout=10)
    except (OSError, ValueError):
        pass
    time.sleep(0.5)
    try:
        os.kill(int(open(PIDF).read().strip()), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    for p in (SOCK, PIDF):
        try:
            os.unlink(p)
        except OSError:
            pass
    return True, "会话已结束"


# ============= 入口 =============
def _state_fields(snapshot, session_status):
    """把会话快照摊平成返回字段。"""
    if not snapshot:
        return {"session": session_status}
    return {
        "session": session_status,
        "view": snapshot.get("view"),
        "view_target": snapshot.get("target") or "",
        "prompt": snapshot.get("prompt") or "",
        "unsaved_changes": bool(snapshot.get("dirty")),
        "pending_confirm": bool(snapshot.get("pending_confirm")),
        "confirm_text": snapshot.get("confirm_text") or "",
    }


def _exec(command, timeout, keep, require_session, answer=False, newline=True, fresh=False):
    """公共执行路径：必要时起会话 → 发命令 → 按需停会话。返回 (resp, err)。"""
    started_here = False
    if not _running():
        if require_session:
            return None, error("会话未运行，请先 action=start", code="not_found")
        good, msg = _start()
        if not good:
            return None, error(msg, code="io_error")
        started_here = True
    try:
        try:
            payload = {"action": "run" if newline else "query", "command": command,
                       "timeout": timeout, "answer": answer, "fresh": fresh}
            r = _rpc(payload, timeout=timeout + 15)
            if r.get("eof"):                    # 设备掉线：自动重连一次再重发
                _stop()
                good, msg = _start()
                if not good:
                    return None, error(f"会话断开且重连失败：{msg}", code="io_error")
                r = _rpc(payload, timeout=timeout + 15)
        except (OSError, ValueError) as e:
            return None, error(f"会话通信失败：{e}", code="io_error")
    finally:
        if not keep and (started_here or _running()):
            _stop()
    return r, None


def _reply(r, keep):
    status = "stopped" if not keep else "running"
    body = {"text": r.get("output", ""),
            "needs_confirm": bool(r.get("needs_confirm")),
            "target": f"{USER}@{HOST}",
            "command": r.get("command")}
    body.update(_state_fields(r.get("state"), status))
    if r.get("needs_confirm"):
        body["confirm_text"] = r.get("confirm_text") or body.get("confirm_text") or ""
        body["hint"] = ("设备正在等待 Y/N 确认，本 skill 不会自动应答：确认无风险后用 "
                        "action=answer（command='y' 或 'n'）")
    if body.get("unsaved_changes"):
        body["hint_unsaved"] = "当前有未保存的配置改动；需要持久化时回用户视图执行 save（会要 Y/N 确认）"
    if not r.get("ok"):
        code = "needs_confirm" if r.get("needs_confirm") else "io_error"
        return _fail(r.get("error") or "执行失败", code=code, extra=body)
    return ok(body)


def run(arguments):
    if not isinstance(arguments, dict):
        return error("arguments 必须是对象", code="invalid_argument")
    action = str(arguments.get("action") or "run").strip().lower()
    command = arguments.get("command")
    timeout = arguments.get("timeout", 25)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        return error("timeout 必须是数字", code="invalid_argument")
    if not 1 <= timeout <= 300:
        return error("timeout 需在 1~300 秒之间", code="invalid_argument")

    # ---- 会话生命周期 ----
    if action == "status":
        if _running():
            r = _rpc({"action": "ping"}, timeout=10)
            body = {"session": "running", "pid": r.get("pid"), "alive": r.get("alive"),
                    "idle_seconds": r.get("idle"), "keepalive": r.get("keepalive"),
                    "host": f"{USER}@{HOST}", "socket": SOCK}
            body.update(_state_fields(r.get("state"), "running"))
            return ok(body)
        return ok({"session": "stopped", "host": f"{USER}@{HOST}"})

    if action == "state":
        if not _running():
            return ok({"session": "stopped", "host": f"{USER}@{HOST}"})
        r = _rpc({"action": "state"}, timeout=10)
        body = {"session": "running", "host": f"{USER}@{HOST}"}
        body.update(_state_fields(r.get("state"), "running"))
        return ok(body)

    if action == "start":
        good, msg = _start()
        if not good:
            return error(msg, code="io_error")
        r = _rpc({"action": "ping"}, timeout=10)
        body = {"session": "running", "message": msg, "host": f"{USER}@{HOST}"}
        body.update(_state_fields(r.get("state"), "running"))
        return ok(body)

    if action == "stop":
        good, msg = _stop()
        return ok({"session": "stopped", "message": msg}) if good else error(msg, code="io_error")

    # ---- 视图导航 ----
    if action in ("up", "top"):
        if not _running():
            return error("会话未运行，请先 action=start", code="not_found")
        snap = (_rpc({"action": "state"}, timeout=10) or {}).get("state") or {}
        view = snap.get("view")
        if snap.get("pending_confirm"):
            return _fail("设备正在等待 Y/N 确认：请先 action=answer（或 action=stop）",
                         code="needs_confirm", extra=_state_fields(snap, "running"))
        if view == "user" and action == "up":
            return _fail("已在用户视图：再 quit 会断开 console（要结束会话请用 action=stop）",
                         code="invalid_argument", extra=_state_fields(snap, "running"))
        if view == "user":
            return ok(dict({"text": "", "message": "已在用户视图"},
                           **_state_fields(snap, "running")))
        r, err = _exec("quit" if action == "up" else "return", timeout, True, require_session=True)
        return err if err else _reply(r, True)

    if action == "answer":
        if not isinstance(command, str) or not command.strip():
            return error("answer 需要 command（如 'y' / 'n'）", code="missing_argument")
        if not _running():
            return error("会话未运行，请先 action=start", code="not_found")
        snap = (_rpc({"action": "state"}, timeout=10) or {}).get("state") or {}
        if not snap.get("pending_confirm"):
            return _fail("当前没有待应答的 Y/N 提示；只是要执行命令请用 action=run",
                         code="invalid_argument", extra=_state_fields(snap, "running"))
        r, err = _exec(command, timeout, True, require_session=True, answer=True)
        return err if err else _reply(r, True)

    # ---- 设备侧下一步提示（`?` 在线帮助，由设备回答，非本地硬编码） ----
    if action in ("next", "help"):
        keep = _as_bool(arguments.get("keep"), True)
        prefix = str(arguments.get("prefix") or "")
        if any(ch in prefix for ch in "\r\n"):
            return error("prefix 不能包含换行", code="invalid_argument")
        try:
            page = int(arguments.get("page") or 1)
            size = int(arguments.get("limit") or 10)
        except (TypeError, ValueError):
            return error("page / limit 必须是整数", code="invalid_argument")
        size = max(1, min(size, 50))
        # 原样拼接 "?"：prefix="display " 查子命令；prefix="disp" 查半截命令的补全
        cmd = prefix + "?"
        r, err = _exec(cmd, min(timeout, 30), keep, require_session=False,
                       newline=False, fresh=_as_bool(arguments.get("fresh"), False))
        if err:
            return err
        if not r.get("ok"):
            return _reply(r, keep)
        cmds = _parse_help(r.get("output", ""))
        total = len(cmds)
        pages = max(1, (total + size - 1) // size)
        if page < 1 or page > pages:
            return _fail(f"page 超出范围（1~{pages}，共 {total} 条）",
                         code="invalid_argument",
                         extra={"total": total, "page_size": size, "total_pages": pages})
        start = (page - 1) * size
        body = {"command": cmd, "total": total, "page": page, "page_size": size,
                "total_pages": pages, "has_more": page < pages,
                "next_steps": cmds[start:start + size],
                "target": f"{USER}@{HOST}"}
        if page < pages:
            body["next_page_args"] = {"action": "next", "prefix": prefix, "page": page + 1}
        body.update(_state_fields(r.get("state"), "running" if keep else "stopped"))
        if _as_bool(arguments.get("raw"), False):
            body["text"] = r.get("output", "")
        return ok(body)

    # ---- 执行命令 ----
    if action in ("run", "send"):
        if not isinstance(command, str) or not command.strip():
            return error("command 必填（字符串）", code="missing_argument")
        keep = _as_bool(arguments.get("keep"), True)
        r, err = _exec(command, timeout, keep, require_session=(action == "send"))
        return err if err else _reply(r, keep)

    return error(f"未知 action '{action}'（应为 run/send/start/stop/status/state/up/top/answer/next）",
                 code="unknown_action")
