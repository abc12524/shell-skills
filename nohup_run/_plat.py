# nohup_run 平台层：进程存活判定 / 进程树终止 / 解释器与 shell 选择 / 日志解码
"""三个后端，同一套接口，`skill.py` 与 `_worker.py` 共用（只依赖标准库）。

  linux    /proc/<pid>/stat 取 (state, starttime)；os.killpg 杀树
  macos    kill(pid, 0) 探活，ps -o lstart= 校验身份；os.killpg 杀树
  windows  OpenProcess + GetProcessTimes 校验身份；Job Object 杀树，
           取不到作业对象时退化 taskkill /T /F

设计取舍：
- "start"（启动签名）只做等值比较，用来防 pid 复用；三平台格式各不相同，不跨平台比较。
- Windows 没有 SIGTERM 语义，`hard` 参数被忽略：Job Object / taskkill /F 都是硬终止。
- macOS 的 `alive(verify=False)` 不查 ps（成本高）；`status` / `kill` 这类低频路径传 verify=True。
"""

import os
import shutil
import subprocess
import sys

IS_WIN = os.name == "nt"
IS_DARWIN = sys.platform == "darwin"

if IS_WIN:
    PLATFORM = "windows"
elif IS_DARWIN:
    PLATFORM = "macos"
elif sys.platform == "linux":
    PLATFORM = "linux"
else:
    PLATFORM = sys.platform


# ------------------------------------------------------------------ Windows ---
if IS_WIN:
    import ctypes
    from ctypes import wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _QUERY_LIMITED = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _STILL_ACTIVE = 259
    _JOB_TERMINATE = 0x0008
    _JOB_QUERY = 0x0004
    _JOB_EXT_LIMIT = 9
    _KILL_ON_JOB_CLOSE = 0x2000
    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wt.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wt.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wt.DWORD),
            ("SchedulingClass", wt.DWORD),
        ]

    class _EXT_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32.OpenProcess.restype = wt.HANDLE
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _k32.GetProcessTimes.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(wt.FILETIME),
        ctypes.POINTER(wt.FILETIME),
        ctypes.POINTER(wt.FILETIME),
        ctypes.POINTER(wt.FILETIME),
    ]
    _k32.CreateJobObjectW.restype = wt.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _k32.OpenJobObjectW.restype = wt.HANDLE
    _k32.OpenJobObjectW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    _k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]

    _k32.IsProcessInJob.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL)]
    _k32.GetCurrentProcess.restype = wt.HANDLE

    _JOBS = {}  # job 名 → 句柄：worker 全程持有，进程退出时 OS 自动关闭 → 子树连带回收

    def _filetime(ft):
        return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)

    def _start_from_handle(handle):
        created, exited, kernel, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        okp = _k32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        return "%X:%08X" % (created.dwHighDateTime, created.dwLowDateTime) if okp else None

    def _open(pid, access):
        try:
            return _k32.OpenProcess(access, False, int(pid)) or None
        except (OverflowError, ValueError, TypeError):
            return None

    def taskkill(pid, force=True):
        argv = ["taskkill", "/PID", str(int(pid)), "/T"]
        if force:
            argv.append("/F")
        try:
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def make_job(name):
        """建命名作业对象并开 KILL_ON_JOB_CLOSE；失败返回 None（调用方退化 taskkill）。"""
        handle = _k32.CreateJobObjectW(None, name)
        if not handle:
            return None
        info = _EXT_LIMIT()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(
            handle, _JOB_EXT_LIMIT, ctypes.byref(info), ctypes.sizeof(info)
        ):
            _k32.CloseHandle(handle)
            return None
        _JOBS[name] = handle
        return handle

    def assign_job(name, pid):
        handle = _JOBS.get(name)
        # AssignProcessToJobObject 需要 PROCESS_SET_QUOTA + PROCESS_TERMINATE
        proc = _open(pid, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE)
        if not handle or not proc:
            if proc:
                _k32.CloseHandle(proc)
            return False
        try:
            return bool(_k32.AssignProcessToJobObject(handle, proc))
        finally:
            _k32.CloseHandle(proc)

    def in_job():
        """当前进程是否被套在某个 Job 里（套着且不许 breakaway，后台任务就活不过调用方）。"""
        flag = wt.BOOL(False)
        if not _k32.IsProcessInJob(_k32.GetCurrentProcess(), None, ctypes.byref(flag)):
            return False
        return bool(flag.value)

    def terminate_job(name):
        handle = _JOBS.get(name)
        if handle:
            return bool(_k32.TerminateJobObject(handle, 1))
        # 跨进程（skill.py 杀任务）：按名字重新打开
        handle = _k32.OpenJobObjectW(_JOB_TERMINATE | _JOB_QUERY, False, name)
        if not handle:
            return False
        try:
            return bool(_k32.TerminateJobObject(handle, 1))
        finally:
            _k32.CloseHandle(handle)


# -------------------------------------------------------------- 启动签名 ---
def start_sig(pid):
    """进程启动签名（等价 /proc 的 starttime），拿不到返回 None。"""
    if not pid:
        return None
    if IS_WIN:
        handle = _open(pid, _QUERY_LIMITED)
        if not handle:
            return None
        try:
            return _start_from_handle(handle)
        finally:
            _k32.CloseHandle(handle)
    if PLATFORM == "linux":
        try:
            with open("/proc/%d/stat" % int(pid), "rb") as f:
                raw = f.read().decode("utf-8", "replace")
        except (OSError, ValueError, TypeError):
            return None
        end = raw.rfind(")")
        if end < 0:
            return None
        fields = raw[end + 2:].split()
        return fields[19] if len(fields) >= 20 else None
    # macOS
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return out.stdout.decode("utf-8", "replace").strip() or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ---------------------------------------------------------------- 存活判定 ---
def alive(pid, start=None, verify=False):
    """进程是否还活着；给了 start 就同时校验（防 pid 复用）。僵尸算死（Linux）。"""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if not pid:
        return False

    if IS_WIN:
        handle = _open(pid, _QUERY_LIMITED)
        if not handle:
            return False  # 不存在，或被保护（不是我们的进程）
        try:
            code = wt.DWORD()
            if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            if code.value != _STILL_ACTIVE:
                return False
            if start is not None and _start_from_handle(handle) != str(start):
                return False
            return True
        finally:
            _k32.CloseHandle(handle)

    if PLATFORM == "linux":
        state, cur = None, None
        try:
            with open("/proc/%d/stat" % pid, "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            end = raw.rfind(")")
            fields = raw[end + 2:].split() if end >= 0 else []
            if len(fields) >= 20:
                state, cur = fields[0], fields[19]
        except (OSError, ValueError):
            return False
        if state is None or state == "Z":
            return False
        if start is not None and str(cur) != str(start):
            return False
        return True

    # macOS：kill(pid, 0) 最省（不 fork ps），需要身份校验时才查一次
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # 进程在，只是不归我们管
    except OSError:
        return False
    if verify and start is not None:
        return start_sig(pid) == str(start)
    return True


# ---------------------------------------------------------------- 终止进程树 ---
def term_tree(status, hard=False):
    """终止整棵任务进程树（一次调用发一轮信号）。status 是 status.json 的内容。

    POSIX  : 先 SIGTERM（hard=False）后 SIGKILL；按进程组杀，覆盖 sh + 子孙
    Windows: Job Object（一次性杀光树）；退化 taskkill /T /F
    """
    status = status or {}
    pid = status.get("pgid") or status.get("child_pid")
    if IS_WIN:
        job = status.get("job")
        if job and terminate_job(job):
            return True
        return taskkill(pid, force=True) if pid else False

    if not pid:
        return False
    import signal as _signal

    sig = _signal.SIGKILL if hard else _signal.SIGTERM
    try:
        os.killpg(int(pid), sig)
        return True
    except (OSError, ValueError, TypeError):
        pass
    try:
        os.kill(int(pid), sig)
        return True
    except (OSError, ValueError, TypeError):
        return False


def signal_worker(pid):
    """通知执行器进程（POSIX 送 SIGTERM，它会先杀子树再落终态）；Windows 直接 TerminateProcess。"""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if not pid:
        return False
    if IS_WIN:
        handle = _open(pid, _PROCESS_TERMINATE)
        if not handle:
            return False
        try:
            return bool(_k32.TerminateProcess(handle, 1))
        finally:
            _k32.CloseHandle(handle)
    import signal as _signal

    try:
        os.kill(pid, _signal.SIGTERM)
        return True
    except OSError:
        return False


# ------------------------------------------------------------- 解释器 / shell ---
def python_exe():
    """挑一个真正的 python 解释器（避免把 shell-tool 自身可执行文件、Store 假壳当解释器）。"""
    cands = [
        os.environ.get("NOHUP_RUN_PYTHON"),
        sys.executable,
        shutil.which("python3"),
        shutil.which("python"),
    ]
    for cand in cands:
        if not cand or "python" not in os.path.basename(cand).lower():
            continue
        if IS_WIN and "windowsapps" in cand.lower():
            continue  # Microsoft Store 别名：跑起来弹商店，不是真解释器
        if not os.path.isfile(cand):
            continue
        if not IS_WIN and not os.access(cand, os.X_OK):
            continue  # Windows 上 X_OK 恒真，只有 POSIX 需要判
        return cand
    return None


def shell_argv(command, shell=None):
    """命令 → (argv, shell 名)。shell 名回报给调用方，便于排查到底用了谁。"""
    if not IS_WIN:
        return ["/bin/sh", "-c", command], "sh"
    kind = str(shell or os.environ.get("NOHUP_RUN_SHELL") or "auto").strip().lower()
    if kind in ("cmd", "cmd.exe", "command"):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", command], "cmd"
    if kind in ("auto", "pwsh", "powershell", "ps"):
        # 指名 powershell 就用 powershell.exe；auto / pwsh / ps 才优先 pwsh（PS7）
        if kind == "powershell":
            exe = shutil.which("powershell") or os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                "WindowsPowerShell", "v1.0", "powershell.exe",
            )
            name = "powershell"
        else:
            exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
            name = "pwsh" if os.path.basename(exe).lower().startswith("pwsh") else "powershell"
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], name
    # 其它值当成自定义 shell 可执行文件
    return [kind, "-Command", command], kind


# ------------------------------------------------------------------ 起进程 ---
def spawn_worker(argv, cwd, log_fd):
    """拉起后台执行器：脱离父进程（父进程退出后继续跑），日志直接落 worker.log。

    返回 (Popen, 警告文本或 None)。Windows 上如果脱离 Job 失败，父进程退出会把执行器一起带走，
    这属于调用方环境限制，靠 spawn 解决不了 —— 显式报给调用方，别让人以为任务还在跑。
    """
    kwargs = dict(
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=log_fd,
        close_fds=True,
    )
    if IS_WIN:
        base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        # 父进程若在 Job 里且允许 breakaway，必须带上才能在父进程退出后存活
        try:
            return subprocess.Popen(argv, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kwargs), None
        except OSError:
            proc = subprocess.Popen(argv, creationflags=base, **kwargs)
            warn = None
            if in_job():
                warn = ("调用方进程被套在不允许 breakaway 的 Job 对象里，后台任务可能随调用方退出被连带终止；"
                        "需要长期存活的任务请在常驻服务进程里 run")
            return proc, warn
    return subprocess.Popen(argv, start_new_session=True, **kwargs), None


def spawn_child(command, cwd, env, logf, shell=None, job_name=None):
    """起真正要跑的命令 → (Popen, 附加的 status 字段)。"""
    argv, shell_name = shell_argv(command, shell)
    extra = {"shell": shell_name}
    kwargs = dict(
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    if IS_WIN:
        proc = subprocess.Popen(argv, creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, **kwargs)
        extra["pgid"] = proc.pid  # 仅作标识，Windows 没有进程组
        if job_name and assign_job(job_name, proc.pid):
            extra["job"] = job_name
        return proc, extra
    proc = subprocess.Popen(argv, start_new_session=True, **kwargs)
    extra["pgid"] = proc.pid  # 新会话首进程即进程组组长
    return proc, extra


# ------------------------------------------------------------------ 日志解码 ---
def decode(raw):
    """output.log 是原始字节，而且可能混编码（UTF-8 的头部 + 子进程按系统代码页输出）。

    先整体试 utf-8；不行就逐行挑（中文 Windows 上正文多半是 GBK）。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:  # 结尾可能正好截在多字节字符中间
        return raw[:-1].decode("utf-8")
    except UnicodeDecodeError:
        pass
    if not IS_WIN:
        return raw.decode("utf-8", "replace")

    parts = []
    for line in raw.splitlines(True):
        for enc in ("utf-8", "mbcs", "gbk", "big5"):
            try:
                parts.append(line.decode(enc))
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            parts.append(line.decode("utf-8", "replace"))
    return "".join(parts)
