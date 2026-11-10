# skill: nohup_run
# description: 后台跑命令（nohup 式）：提交即返回任务 id，再查列表 / 状态 / 日志 / 等待 / 终止；Linux / macOS / Windows
# status: enable
# usage:
#   arguments.action = run | list | status | log | wait | kill | clean
#   run: command（必填）+ 可选 cwd（默认 $HOME，不是进程 cwd）/ name / timeout（秒，0=不限）/ env / shell
#        shell 仅 Windows 生效：powershell（默认，有 pwsh 用 pwsh）| cmd | 其它值当可执行文件
#   status|log|wait|kill: id = 完整 id 或 前缀 / 后缀 / 片段 / name / last；status 可加 log=true；wait 上限 60s（超时不杀任务）
#   list: 可选 state=running|finished|failed|all（默认 all）、limit（默认 20）
#   clean: 可选 older_than（小时，默认 24）、min_age（秒，默认 600，更近的不删除非 confirm）/ id、dry_run、confirm
#   示例: {"action":"run","command":"make -j8 2>&1","cwd":"/data/app","timeout":1800,"name":"build"}
#         {"action":"run","command":"npm ci 2>&1","cwd":"D:/app","name":"npm"}   # Windows 默认走 powershell
#         {"action":"status","id":"build","log":true} / {"action":"log","id":"fc90"}
#   长命令（编译 / 安装 / 下载 / 部署）一律先 run：秒回 id 不阻塞本回合，之后 list / status / wait 轮询。
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plat  # noqa: E402 —— 同目录平台层（存活判定 / 杀树 / 解释器 / shell / 解码）

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

HOME = os.environ.get("NOHUP_RUN_HOME") or os.path.join(os.path.expanduser("~"), ".nohup_run")
JOBS = os.path.join(HOME, "jobs")
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_worker.py")

TERMINAL = ("finished", "failed", "timeout", "killed", "lost")
ACTIVE = ("running", "pending")
DEFAULT_LOG_CHARS = 2000
HARD_LOG_CHARS = 200000
MAX_WAIT = 60.0
LIST_SCAN = 200


# ------------------------------------------------------------------ 小工具 ---
def _now():
    return time.time()


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _dur(seconds):
    """秒 → 人类可读时长（唯一表示，不再附带毫秒副本）。"""
    if seconds is None:
        return None
    sec = max(0.0, float(seconds))
    if sec < 1:
        return "%.2fs" % sec
    if sec < 60:
        return "%.1fs" % sec
    minutes, s = divmod(int(round(sec)), 60)
    if minutes < 60:
        return "%dm%02ds" % (minutes, s)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh%02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd%02dh" % (days, hours)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _clip(text, limit=120):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _job_dirs():
    try:
        names = sorted(os.listdir(JOBS))
    except OSError:
        return []
    dirs = [os.path.join(JOBS, n) for n in names if os.path.isdir(os.path.join(JOBS, n))]
    return dirs[-LIST_SCAN:]


def _resolve(raw):
    """id / 前缀 / 后缀 / 片段 / name / last → job 目录；返回 (dir, err_envelope)。"""
    text = str(raw or "").strip()
    if not text:
        return None, error("缺少 id（run 返回的任务 id；支持完整 id、前缀/后缀/片段、name、last）", code="missing_argument")
    raw = os.path.basename(text.rstrip("/\\"))  # 兼容直接贴 ~/.nohup_run/jobs/<id>
    dirs = _job_dirs()
    if not dirs:
        return None, error("还没有任何后台任务", code="not_found")
    if raw.lower() in ("last", "latest"):
        return dirs[-1], None
    exact = os.path.join(JOBS, raw)
    if os.path.isdir(exact):
        return exact, None

    # 前缀 → 后缀 → 片段：报错里都给出候选，方便模型一次纠对
    for label, pred in (
        ("前缀", lambda jid: jid.startswith(raw)),
        ("后缀", lambda jid: jid.endswith(raw)),
        ("片段", lambda jid: raw in jid),
    ):
        hits = [d for d in dirs if pred(os.path.basename(d))]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, error(
                "id %s %r 匹配多个任务: %s（请给更长或更靠后的片段，或 action=list 看全量 id）"
                % (label, raw, ", ".join(os.path.basename(d) for d in hits[-6:])),
                code="ambiguous",
            )

    named = []
    for job_dir in dirs:
        meta = _read_json(os.path.join(job_dir, "meta.json")) or {}
        if str(meta.get("name") or "") == raw:
            named.append(job_dir)
    if len(named) == 1:
        return named[0], None
    if len(named) > 1:
        return None, error(
            "name %r 匹配多个任务: %s（改用 id 片段）"
            % (raw, ", ".join(os.path.basename(d) for d in named[-6:])),
            code="ambiguous",
        )
    return None, error("找不到任务 %r（action=list 看现有任务）" % raw, code="not_found")


def _read_log(job_dir, chars=DEFAULT_LOG_CHARS):
    """读 output.log → (文本, 总字节数)；chars=0 表示全量（上限 HARD_LOG_CHARS）。"""
    path = os.path.join(job_dir, "output.log")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return "", 0
    size = len(raw)
    text = _plat.decode(raw)
    if chars and chars > 0:
        chars = min(int(chars), HARD_LOG_CHARS)
        if len(text) > chars:
            text = "…（省略前 %d 字符）\n%s" % (len(text) - chars, text[-chars:])
    elif chars == 0:
        text = text[-HARD_LOG_CHARS:]
    return text, size


# ------------------------------------------------------------------ 任务视图 ---
def _view(job_dir, full=False):
    meta = _read_json(os.path.join(job_dir, "meta.json")) or {}
    st = _read_json(os.path.join(job_dir, "status.json")) or {}

    created = meta.get("created_at") or 0
    started = st.get("started_at") or created or None
    state = st.get("state")
    if not state:
        state = "pending" if (created and _now() - created < 5) else "lost"

    orphan = False
    if state == "running" and not _plat.alive(st.get("pid"), st.get("pid_start"), verify=full):
        # 执行器没了：子进程还在就算 orphan（继续跑），否则判 lost
        if _plat.alive(st.get("pgid"), st.get("child_start"), verify=full):
            orphan = True
        else:
            state = "lost"

    ended = st.get("ended_at")
    if state in ACTIVE:
        elapsed = (_now() - started) if started else 0.0
    elif state == "lost":
        # 执行器消失且不知道何时结束：用「至今」做上界估计
        elapsed = (_now() - started) if started else None
    else:
        elapsed = (ended - started) if (ended and started) else None

    view = {
        "id": meta.get("id") or os.path.basename(job_dir),
        "state": state,
        "command": meta.get("command") if full else _clip(meta.get("command")),
        "cwd": meta.get("cwd"),
        "elapsed": _dur(elapsed),
        "log": os.path.join(job_dir, "output.log"),
    }
    if meta.get("name"):
        view["name"] = meta["name"]
    if started:
        view["started_at"] = _iso(started)
    if meta.get("timeout"):
        view["timeout"] = meta["timeout"]
    if state in TERMINAL:
        view["exit_code"] = st.get("exit_code")
    if st.get("timed_out"):
        view["timed_out"] = True
    if orphan:
        view["orphan"] = True
    if st.get("error"):
        view["error"] = st.get("error")
    if full:
        view["worker_log"] = os.path.join(job_dir, "worker.log")
    return view


# --------------------------------------------------------------------- action ---
def _run(a):
    command = a.get("command")
    if not isinstance(command, str) or not command.strip():
        return error("缺少 command（要后台执行的命令）", code="missing_argument")

    # 默认目录不用进程 cwd（服务化运行时往往是 /usr/local/bin 之类），统一落到 $HOME；
    # 需要别的目录就显式传 cwd，或用 NOHUP_RUN_CWD 固定。
    raw_cwd = a.get("cwd") or os.environ.get("NOHUP_RUN_CWD") or os.path.expanduser("~")
    cwd = os.path.abspath(os.path.expanduser(str(raw_cwd)))
    if not os.path.isdir(cwd):
        return error("cwd 不存在: %s" % cwd, code="not_found")

    try:
        timeout = float(a.get("timeout") or 0)
    except (TypeError, ValueError):
        return error("timeout 必须是秒数（0 = 不限）", code="bad_request")
    if timeout < 0:
        return error("timeout 不能为负", code="bad_request")

    env = a.get("env") or {}
    if not isinstance(env, dict):
        return error("env 必须是对象（键值对）", code="bad_request")

    python = _plat.python_exe()
    if not python:
        return error("找不到可用的 python3 解释器（可用 NOHUP_RUN_PYTHON 指定）", code="not_found")

    job_id = "n" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    job_dir = os.path.join(JOBS, job_id)
    for path in (HOME, JOBS):
        os.makedirs(path, mode=0o700, exist_ok=True)
    os.makedirs(job_dir, mode=0o700, exist_ok=True)

    created = _now()
    shell = str(a.get("shell") or "").strip() or None
    meta = {
        "id": job_id,
        "command": command,
        "cwd": cwd,
        "env": {str(k): ("" if v is None else str(v)) for k, v in env.items()},
        "timeout": timeout,
        "shell": shell,
        "name": (str(a.get("name") or "").strip()[:60] or None),
        "created_at": created,
        "created_at_iso": _iso(created),
        "platform": _plat.PLATFORM,
    }
    _write_json(os.path.join(job_dir, "meta.json"), meta)

    worker_log = os.path.join(job_dir, "worker.log")
    fd = os.open(worker_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _, detached_warn = _plat.spawn_worker([python, WORKER, job_dir], job_dir, fd)
    except OSError as exc:
        return error("启动后台执行器失败: %s" % exc, code="spawn_failed")
    finally:
        os.close(fd)

    # 等执行器落下第一份 status（正常 <50ms）；起不来就带 worker.log 报错，别留幽灵任务
    status_path = os.path.join(job_dir, "status.json")
    deadline = _now() + 2.0
    while _now() < deadline and not os.path.exists(status_path):
        time.sleep(0.05)
    if not os.path.exists(status_path):
        try:
            with open(worker_log, "rb") as f:
                detail = _plat.decode(f.read()[-1200:]) or "（worker.log 为空）"
        except OSError:
            detail = "（读不到 worker.log）"
        return error("后台任务未能启动，见 %s" % worker_log, code="spawn_failed", detail=detail)

    view = _view(job_dir)
    if view["state"] in ACTIVE:
        view["next"] = "action=status id=%s（或 list / wait）" % job_id
    if detached_warn:
        view["warning"] = detached_warn
    return ok(view)


def _list(a):
    state = str(a.get("state") or "all").strip().lower()
    if state not in ("all", "running", "finished", "failed"):
        return error("state 只能是 running / finished / failed / all", code="bad_request")
    try:
        limit = int(a.get("limit") or 20)
    except (TypeError, ValueError):
        return error("limit 必须是整数", code="bad_request")
    limit = max(1, min(limit, LIST_SCAN))

    views = [_view(d) for d in _job_dirs()]
    running = sum(1 for v in views if v["state"] in ACTIVE)
    if state == "running":
        views = [v for v in views if v["state"] in ACTIVE]
    elif state == "finished":
        views = [v for v in views if v["state"] == "finished"]
    elif state == "failed":
        views = [v for v in views if v["state"] in ("failed", "timeout", "killed", "lost")]
    views.reverse()

    return ok({
        "count": len(views),
        "running": running,
        "jobs": views[:limit],
        "home": HOME,
    })


def _status(a):
    job_dir, err = _resolve(a.get("id"))
    if err:
        return err
    view = _view(job_dir, full=True)
    if a.get("log"):
        try:
            chars = int(a.get("chars") if a.get("chars") is not None else DEFAULT_LOG_CHARS)
        except (TypeError, ValueError):
            return error("chars 必须是整数（0 = 不截断）", code="bad_request")
        view["output"] = _read_log(job_dir, chars)[0]
    return ok(view)


def _log(a):
    job_dir, err = _resolve(a.get("id"))
    if err:
        return err
    try:
        chars = int(a.get("chars") if a.get("chars") is not None else DEFAULT_LOG_CHARS)
    except (TypeError, ValueError):
        return error("chars 必须是整数（0 = 不截断）", code="bad_request")
    view = _view(job_dir)
    text, size = _read_log(job_dir, chars)
    return ok({"id": view["id"], "state": view["state"], "bytes": size, "output": text})


def _wait(a):
    job_dir, err = _resolve(a.get("id"))
    if err:
        return err
    try:
        timeout = float(a.get("timeout") if a.get("timeout") is not None else 10)
    except (TypeError, ValueError):
        return error("timeout 必须是秒数", code="bad_request")
    timeout = max(0.0, min(timeout, MAX_WAIT))

    deadline = _now() + timeout
    interval = 0.25
    while True:
        view = _view(job_dir)
        if view["state"] in TERMINAL:
            return ok(view)
        if _now() >= deadline:
            view["waited"] = _dur(timeout)
            view["next"] = "仍在运行：先做别的事，稍后再 wait / status（单次最长 %ds，只查询不杀任务）" % int(MAX_WAIT)
            return ok(view)
        time.sleep(interval)
        interval = min(interval * 1.5, 2.0)


def _kill(a):
    job_dir, err = _resolve(a.get("id"))
    if err:
        return err
    view = _view(job_dir)
    if view["state"] not in ACTIVE:
        view["killed"] = False
        view["next"] = "任务已结束，无需终止"
        return ok(view)

    st = _read_json(os.path.join(job_dir, "status.json")) or {}
    hard = str(a.get("signal") or "").strip().upper() in ("KILL", "9", "SIGKILL")
    target = st.get("pgid") or st.get("child_pid")

    # 先落标记：Windows 里被杀进程的退出码是正整数（1），跟普通失败分不开，靠它纠正成 killed
    try:
        with open(os.path.join(job_dir, "kill.flag"), "w", encoding="utf-8") as f:
            f.write("%.0f\n" % _now())
    except OSError:
        pass

    # 先礼后兵：POSIX 走 SIGTERM → SIGKILL；Windows 没有 SIGTERM 语义，一步到硬终止（Job → taskkill）
    _plat.term_tree(st, hard=hard)
    deadline = _now() + 2.0
    while _now() < deadline and _plat.alive(target, st.get("child_start"), verify=True):
        time.sleep(0.1)
    if _plat.alive(target, st.get("child_start"), verify=True):
        _plat.term_tree(st, hard=True)

    # 等执行器自己落终态（子进程一死它就会写 killed，别抢在它前面把它打掉）
    view = _view(job_dir)
    deadline = _now() + 3.0
    while _now() < deadline and view["state"] not in TERMINAL:
        time.sleep(0.1)
        view = _view(job_dir)

    # 执行器还赖着不写终态：兜底通知它（POSIX SIGTERM / Windows TerminateProcess）
    if view["state"] not in TERMINAL and _plat.alive(st.get("pid"), st.get("pid_start"), verify=True):
        _plat.signal_worker(st.get("pid"))
        deadline = _now() + 2.0
        while _now() < deadline and view["state"] not in TERMINAL:
            time.sleep(0.1)
            view = _view(job_dir)

    # 还不落终态就自己写，别留一个永远 running / lost 的任务
    if view["state"] not in TERMINAL:
        st.update({
            "state": "killed",
            "ended_at": _now(),
            "ended_at_iso": _iso(_now()),
            "forced": True,
            "note": "执行器未在超时内落终态，由 kill 强制标记",
        })
        _write_json(os.path.join(job_dir, "status.json"), st)
        view = _view(job_dir)

    view["killed"] = view["state"] in TERMINAL
    return ok(view)


def _clean(a):
    try:
        hours = float(a.get("older_than") if a.get("older_than") is not None else 24)
        min_age = float(a.get("min_age") if a.get("min_age") is not None else 600)
    except (TypeError, ValueError):
        return error("older_than（小时）/ min_age（秒）必须是数字", code="bad_request")
    if hours < 0 or min_age < 0:
        return error("older_than / min_age 不能为负", code="bad_request")
    dry_run = bool(a.get("dry_run", False))
    confirm = bool(a.get("confirm", False))

    target = None
    if a.get("id"):
        target, err = _resolve(a.get("id"))
        if err:
            return err

    removed = []
    recent = []
    for job_dir in _job_dirs():
        if target and job_dir != target:
            continue
        view = _view(job_dir)
        if view["state"] in ACTIVE:
            continue
        meta = _read_json(os.path.join(job_dir, "meta.json")) or {}
        age = _now() - float(meta.get("created_at") or 0)
        if not target:
            if age < hours * 3600:
                continue
            # 防手滑：older_than=0 也不动 min_age 内刚跑完的任务，除非显式 confirm
            if not confirm and age < min_age:
                recent.append(view["id"])
                continue
        removed.append(view["id"])
        if not dry_run:
            shutil.rmtree(job_dir, ignore_errors=True)

    result = {"removed": removed, "count": len(removed), "dry_run": dry_run, "home": HOME}
    if recent:
        result["skipped_recent"] = recent[:10]
        result["note"] = "有 %d 个任务在 min_age=%ds 内刚结束，未删除；确认要删就加 confirm=true" % (
            len(recent), int(min_age),
        )
    return ok(result)


_ACTIONS = {
    "run": _run,
    "list": _list,
    "status": _status,
    "log": _log,
    "wait": _wait,
    "kill": _kill,
    "clean": _clean,
}


def run(arguments):
    a = arguments or {}
    action = str(a.get("action") or "").strip()
    fn = _ACTIONS.get(action)
    if not fn:
        return error(
            "未知 action '%s'，可用: %s" % (action, ", ".join(_ACTIONS)),
            code="unknown_action",
        )
    return fn(a)


if __name__ == "__main__":
    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    result = run(payload)
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
