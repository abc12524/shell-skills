#!/usr/bin/env python3
"""nohup_run 的后台执行器：独立进程里跑一条 `sh -c` 命令，把状态写回 job 目录。

由 `skill.py` 这样启动（`start_new_session=True`，脱离调用方的会话与进程组）：

    python3 _worker.py <job_dir>

job 目录内容：

    meta.json     输入（skill.py 写）：id / command / cwd / env / timeout / name / created_at
    status.json   输出（本进程原子替换）：state / pid / pgid / 起止时间 / exit_code
    output.log    命令的 stdout + stderr（直接写文件，不经过管道，无缓冲风险）
    worker.log    本进程自己的 stdout/stderr：启动失败或异常堆栈都落在这里

设计要点：
- 不导入技能包、不依赖 shell-tool，只用标准库，可单独 `python3 _worker.py <dir>` 运行。
- 子进程再 `start_new_session=True`：`kill` 可按进程组整棵树终止（sh + 其子孙）。
- 记录 `/proc/<pid>/stat` 的 starttime，供 `status` 判定 pid 复用，避免把别人的进程当成任务。
- 收到 SIGTERM（`kill` 兜底路径）先杀子进程组，再落一个终态 status 后退出。
"""

import json
import os
import signal
import subprocess
import sys
import time
import traceback

STATE = {"job_dir": None, "status": {}, "done": False}


# ------------------------------------------------------------------ 小工具 ---
def now():
    return time.time()


def iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def write_json(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def proc_info(pid):
    """读 /proc/<pid>/stat → (state, starttime)；进程不存在返回 (None, None)。"""
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None, None
    end = raw.rfind(")")
    if end < 0:
        return None, None
    fields = raw[end + 2:].split()
    if len(fields) < 20:
        return None, None
    return fields[0], fields[19]


def kill_group(pgid, sig):
    try:
        os.killpg(int(pgid), sig)
        return True
    except (OSError, ValueError, TypeError):
        return False


def save(**extra):
    st = STATE["status"]
    st.update(extra)
    write_json(os.path.join(STATE["job_dir"], "status.json"), st)


def finalize(state, exit_code=None, error=None, timed_out=False, note=None):
    """落终态（幂等：信号处理器与正常路径只会有一次生效）。"""
    if STATE["done"]:
        return
    STATE["done"] = True
    st = STATE["status"]
    end = now()
    started = st.get("started_at") or end
    st.update({
        "state": state,
        "ended_at": end,
        "ended_at_iso": iso(end),
        "duration_ms": int((end - started) * 1000),
        "exit_code": exit_code,
        "timed_out": bool(timed_out),
    })
    if error:
        st["error"] = error
    if note:
        st["note"] = note
    try:
        write_json(os.path.join(STATE["job_dir"], "status.json"), st)
    except OSError:
        pass


def on_signal(signum, _frame):
    pgid = STATE["status"].get("pgid")
    if pgid:
        kill_group(pgid, signal.SIGTERM)
        time.sleep(0.5)
        kill_group(pgid, signal.SIGKILL)
    finalize("killed", note="worker 收到信号 %d" % signum)
    os._exit(128 + signum)


# --------------------------------------------------------------------- 主流程 ---
def main(argv):
    if len(argv) < 2:
        sys.stderr.write("用法: python3 _worker.py <job_dir>\n")
        return 2

    job_dir = os.path.abspath(argv[1])
    STATE["job_dir"] = job_dir
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, on_signal)

    with open(os.path.join(job_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)

    started = now()
    STATE["status"] = {
        "id": meta.get("id"),
        "state": "running",
        "pid": os.getpid(),
        "pid_start": proc_info(os.getpid())[1],
        "started_at": started,
        "started_at_iso": iso(started),
        "command": meta.get("command"),
        "cwd": meta.get("cwd"),
        "timeout": float(meta.get("timeout") or 0),
        "pgid": None,
        "child_pid": None,
        "child_start": None,
        "exit_code": None,
    }
    save()

    cwd = meta.get("cwd") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        finalize("failed", error="cwd 不存在: %s" % cwd)
        return 1

    env = os.environ.copy()
    for key, value in (meta.get("env") or {}).items():
        env[str(key)] = "" if value is None else str(value)
    env["NOHUP_RUN_ID"] = str(meta.get("id") or "")
    env["NOHUP_RUN_JOB_DIR"] = job_dir

    timeout = float(meta.get("timeout") or 0)
    command = str(meta.get("command") or "")
    log_path = os.path.join(job_dir, "output.log")

    with open(log_path, "ab", buffering=0) as logf:
        header = "# nohup_run %s  %s\n# cwd: %s\n# cmd: %s\n" % (
            meta.get("id"), iso(started), cwd, command,
        )
        logf.write(header.encode("utf-8", "replace"))
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            logf.write(("# 启动子进程失败: %s\n" % exc).encode("utf-8", "replace"))
            finalize("failed", error="启动子进程失败: %s" % exc)
            return 1

        save(child_pid=proc.pid, child_start=proc_info(proc.pid)[1], pgid=proc.pid)

        code = None
        timed_out = False
        try:
            code = proc.wait(timeout=timeout if timeout > 0 else None)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_group(proc.pid, signal.SIGTERM)
            try:
                code = proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                kill_group(proc.pid, signal.SIGKILL)
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = None

        end = now()
        if timed_out:
            state = "timeout"
        elif code is None:
            state = "lost"
        elif code == 0:
            state = "finished"
        elif code < 0:
            state = "killed"
        else:
            state = "failed"
        logf.write(("# %s exit=%s duration=%.1fs\n" % (state, code, end - started)).encode())

    finalize(state, exit_code=code, timed_out=timed_out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 —— 任何异常都要落盘，不能静默消失
        text = traceback.format_exc()
        sys.stderr.write(text)
        try:
            if STATE.get("job_dir"):
                finalize("failed", error=text.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
