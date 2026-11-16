"""audio_play 后端层：设备枚举 + 播放启动（仅标准库，import 期零副作用）。

每个后端都提供 devices() / play(path, device)，同一后端既枚举又播放，
保证 list 返回的 index / id 与 play 用的是同一套坐标。

后端优先级（第一个 available() 的生效）：

  linux    pulse（pactl + paplay / pw-play）→ mpv → ffplay
  macos    mpv → afplay
  windows  mpv → ffplay → shelldefault（交给系统默认播放器）

设备选择能力：只有 pulse / mpv 能把声音送到指定设备；ffplay / afplay / shelldefault
只认系统默认设备，指定其它设备时返回 device_not_supported 并给出解决建议。
"""

import glob
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import json

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

DEFAULT_NAME = "系统默认设备"
DEFAULT_LABEL = "自动（系统默认）"


class BackendError(Exception):
    """后端执行失败：message 直接进信封，code 是稳定错误码。"""

    def __init__(self, message, code="backend_error"):
        super().__init__(message)
        self.message = message
        self.code = code


# ------------------------------------------------------------------ 基础工具 ---
def _which(*names):
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return ""


def _decode(raw):
    if not raw:
        return ""
    candidates = ["utf-8", locale.getpreferredencoding(False), "cp936", "latin-1"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _audio_env():
    """子进程环境：Linux 上补上 PulseAudio / PipeWire 的 socket 路径。

    服务进程（shell-tool / 守护进程）常在没有登录会话时启动，XDG_RUNTIME_DIR 为空，
    pactl / paplay 找不到 /run/user/<uid>/pulse/native，只报「连接失败」。
    这里按 XDG_RUNTIME_DIR → /run/user/<uid> → 系统级 socket 的顺序探测并补齐。
    """
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    env = dict(os.environ)
    if IS_WIN or IS_MAC or env.get("PULSE_SERVER"):
        _ENV_CACHE = env
        return env
    uid = os.getuid() if hasattr(os, "getuid") else None
    runtime = env.get("XDG_RUNTIME_DIR") or ""
    candidates = []
    if runtime:
        candidates.append(os.path.join(runtime, "pulse", "native"))
    if uid is not None:
        candidates.append("/run/user/%d/pulse/native" % uid)
    candidates += sorted(glob.glob("/run/user/*/pulse/native"))
    candidates += ["/run/pulse/native", "/var/run/pulse/native"]
    for path in candidates:
        if not os.path.exists(path):
            continue
        env["PULSE_SERVER"] = "unix:" + path
        if not env.get("XDG_RUNTIME_DIR") and path.startswith("/run/user/"):
            env["XDG_RUNTIME_DIR"] = os.path.dirname(os.path.dirname(path))
        break
    _ENV_CACHE = env
    return env


_ENV_CACHE = None


def _run(cmd, timeout=20, env=None):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, env=env if env is not None else _audio_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def _read_tail(path, limit=400):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return ""
    text = _decode(data).strip()
    return text[-limit:]


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _spawn(cmd, env=None, watch=0.4):
    """脱离父进程启动播放器，不等播放结束。

    watch 秒内如果播放器已经异常退出，把 stderr 尾部带回去——常见于
    文件损坏 / 编码不支持 / 后端参数不被接受；正常播放则直接返回进程。
    """
    log = tempfile.NamedTemporaryFile(prefix="audio_play_", suffix=".log", delete=False)
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=log, stdin=subprocess.DEVNULL,
                  env=env if env is not None else _audio_env())
    if IS_WIN:
        # CREATE_NO_WINDOW 不弹控制台；CREATE_NEW_PROCESS_GROUP 便于与父进程解耦
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        log.close()
        _unlink(log.name)
        raise BackendError("启动播放器失败: %s" % exc, code="spawn_failed")
    time.sleep(watch)
    code = proc.poll()
    log.close()
    if code is None:
        return proc  # 还在播：日志文件留给系统临时目录
    detail = _read_tail(log.name)
    _unlink(log.name)
    if code != 0:
        raise BackendError(
            "播放失败（退出码 %s）: %s" % (code, detail or "无输出"),
            code="play_failed",
        )
    return proc  # 极短文件在 watch 内已正常播完


def _default_device(index=0):
    return {"index": index, "id": "", "name": DEFAULT_NAME, "default": True}


def _cleanup_logs(max_age=3600):
    """顺手清掉上次播放留下的 stderr 日志（正在写入 / 被占用的跳过）。"""
    try:
        folder = tempfile.gettempdir()
        now = time.time()
        for name in os.listdir(folder):
            if not name.startswith("audio_play_") or not name.endswith(".log"):
                continue
            path = os.path.join(folder, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.unlink(path)
            except OSError:
                pass
    except OSError:
        pass


def _index_devices(items):
    for i, item in enumerate(items):
        item["index"] = i
    return items


# -------------------------------------------------------------------- 后端 ---
class Backend:
    name = "base"
    can_select = False

    @classmethod
    def available(cls):
        return False

    def devices(self):
        raise BackendError("后端 %s 不支持枚举设备" % self.name, code="unsupported")

    def play(self, path, device):
        raise BackendError("后端 %s 不支持播放" % self.name, code="unsupported")

    def _require_default(self, device):
        if device and not device.get("default"):
            raise BackendError(
                "后端 %s 只能输出到系统默认设备；指定其它设备请安装 mpv" % self.name,
                code="device_not_supported",
            )


def _pulse_env():
    """pactl 输出按 C locale 解析（中文环境下键名会变成「名称：/描述：」，无法辨认）。"""
    env = dict(_audio_env())
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["LANGUAGE"] = "C"
    return env


def _pulse_sink_list():
    """pactl list sinks 解析（PulseBackend 与 Linux 上的 ffplay 共用）。"""
    code, out, err = _run([_which("pactl"), "list", "sinks"], env=_pulse_env())
    if code != 0 and not out.strip():
        hint = ""
        if "连接" in err or "connect" in err.lower():
            hint = ("（PulseAudio/PipeWire 连接失败：检查 PULSE_SERVER / XDG_RUNTIME_DIR "
                    "是否指向 /run/user/<uid>/pulse/native）")
        raise BackendError("pactl 枚举失败: %s%s" % (err.strip() or "无输出", hint),
                           code="backend_error")
    default = _pulse_default_sink()
    items = []
    block = {}
    for line in out.splitlines() + ["Sink #"]:
        if line.startswith("Sink #"):
            name = block.get("Name")
            if name:
                items.append({
                    "id": name,
                    "name": block.get("Description") or name,
                    "default": name == default,
                })
            block = {}
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            if key in ("Name", "Description") and key not in block:
                block[key] = value.strip()
    if not items and out.strip():
        raise BackendError("pactl 输出无法解析（不是预期的 sinks 列表）", code="backend_error")
    return _index_devices(items)


def _pulse_default_sink():
    env = _pulse_env()
    code, out, _ = _run([_which("pactl"), "get-default-sink"], env=env)
    if code == 0 and out.strip():
        return out.strip().splitlines()[0].strip()
    code, out, _ = _run([_which("pactl"), "info"], env=env)
    match = re.search(r"^Default Sink:\s*(.+)$", out, re.MULTILINE)
    return match.group(1).strip() if match else ""


class PulseBackend(Backend):
    """Linux：PulseAudio / PipeWire（pactl 枚举，paplay 或 pw-play 播放）。"""

    name = "pulse"
    can_select = True

    @classmethod
    def available(cls):
        return bool(_which("pactl")) and bool(_which("paplay", "pw-play"))

    def devices(self):
        return _pulse_sink_list()

    def play(self, path, device):
        dev = (device or {}).get("id") or ""
        paplay = _which("paplay")
        if paplay:
            cmd = [paplay] + (["--device=%s" % dev] if dev else []) + [path]
        else:
            cmd = [_which("pw-play")] + (["--target=%s" % dev] if dev else []) + [path]
        return _spawn(cmd)


class MpvBackend(Backend):
    """跨平台：mpv（设备 id 形如 wasapi/{GUID} / coreaudio/... / pulse/...）。"""

    name = "mpv"
    can_select = True

    @classmethod
    def available(cls):
        return bool(_which("mpv"))

    def devices(self):
        code, out, err = _run([_which("mpv"), "--audio-device=help"])
        if code != 0:
            raise BackendError("mpv 枚举设备失败: %s" % (err.strip() or "无输出"), code="backend_error")
        items = []
        for line in out.splitlines():
            match = re.match(r"\s*'([^']+)'\s*(?:\((.*)\))?\s*$", line)
            if not match:
                continue
            dev_id = match.group(1)
            label = (match.group(2) or "").strip()
            items.append({
                "id": dev_id,
                "name": label or dev_id,
                "default": dev_id == "auto",
            })
        if not items:
            items = [{"id": "auto", "name": DEFAULT_LABEL, "default": True}]
        return _index_devices(items)

    def play(self, path, device):
        dev = (device or {}).get("id") or "auto"
        cmd = [_which("mpv"), "--no-video", "--really-quiet", "--audio-device=%s" % dev, path]
        return _spawn(cmd)


class FfplayBackend(Backend):
    """ffplay：Windows 枚举真实端点但只能输出默认设备；
    Linux 上有 pactl 时可用 PULSE_SINK 精确选 sink（没有 paplay 的兜底）。"""

    name = "ffplay"
    can_select = False

    @classmethod
    def available(cls):
        return bool(_which("ffplay"))

    def devices(self):
        if IS_WIN:
            return _win_devices()
        if not IS_MAC and _which("pactl"):
            sinks = _pulse_sink_list()
            if sinks:
                self.can_select = True  # PULSE_SINK 可精确指定 sink
                return sinks
        return [_default_device()]

    def play(self, path, device):
        dev = (device or {}).get("id") or ""
        env = None
        if dev and self.can_select:
            env = dict(_audio_env())
            env["PULSE_SINK"] = dev
        elif dev:
            self._require_default(device)
        cmd = [_which("ffplay"), "-nodisp", "-autoexit", "-loglevel", "error", "-i", path]
        return _spawn(cmd, env)


class AfplayBackend(Backend):
    """macOS 内置 afplay：枚举真实输出设备（mpv 缺席时仅默认设备可播）。"""

    name = "afplay"
    can_select = False

    @classmethod
    def available(cls):
        return IS_MAC and bool(_which("afplay"))

    def devices(self):
        return _mac_devices()

    def play(self, path, device):
        self._require_default(device)
        return _spawn([_which("afplay"), path])


class ShellDefaultBackend(Backend):
    """Windows 兜底：交给系统默认播放器（会弹出播放器窗口）。"""

    name = "shelldefault"
    can_select = False

    @classmethod
    def available(cls):
        return IS_WIN

    def devices(self):
        return [_default_device()]

    def play(self, path, device):
        self._require_default(device)
        try:
            os.startfile(path)  # noqa: S606 —— Windows 专用，交给默认关联程序
        except OSError as exc:
            raise BackendError("启动系统播放器失败: %s" % exc, code="spawn_failed")
        return None


# ------------------------------------------------------- Windows 端点枚举 ---
_PS_ENUM = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$src = @'
using System;
using System.Runtime.InteropServices;

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
class MMDeviceEnumeratorComObject { }

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppDevice);
}

[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
    int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
    int OpenPropertyStore(int stgmAccess, out IntPtr ppProperties);
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string ppstrId);
    int GetState(out int pdwState);
}

public static class AudioPlayDefault {
    public static string Get() {
        string id = "";
        try {
            var e = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
            IMMDevice dev;
            if (e.GetDefaultAudioEndpoint(0, 0, out dev) == 0 && dev != null) dev.GetId(out id);
        } catch { }
        return id ?? "";
    }
}
'@
try { Add-Type -Language CSharp -TypeDefinition $src -ErrorAction Stop } catch { }
$default = ''
try { $default = [AudioPlayDefault]::Get() } catch { }
$items = @()
try {
    $items = @(Get-PnpDevice -Class AudioEndpoint -Status OK | ForEach-Object {
        [pscustomobject]@{ id = $_.InstanceId; name = $_.FriendlyName }
    })
} catch {
    try {
        $items = @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='AudioEndpoint'" | ForEach-Object {
            [pscustomobject]@{ id = $_.DeviceID; name = $_.Name }
        })
    } catch { }
}
[pscustomobject]@{ default = $default; devices = @($items) } | ConvertTo-Json -Compress -Depth 4
"""


def _json_object(text):
    text = (text or "").strip()
    if not text:
        return {}
    for chunk in [text] + list(reversed(text.splitlines())):
        try:
            value = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _win_devices():
    exe = _which("powershell", "pwsh")
    if not exe:
        return [_default_device()]
    code, out, err = _run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-Command", _PS_ENUM], timeout=40)
    payload = _json_object(out)
    if not payload:
        raise BackendError("枚举音频设备失败: %s" % (err.strip() or out.strip() or "无输出"),
                           code="backend_error")
    default_id = str(payload.get("default") or "").strip().lower()
    items = []
    for raw in payload.get("devices") or []:
        dev_id = str(raw.get("id") or "").strip()
        if "{0.0.0." not in dev_id.lower():
            continue  # 只要渲染（输出）端点，{0.0.1.} 是录音端点
        marker = "MMDEVAPI\\"
        if marker in dev_id.upper():
            dev_id = dev_id[dev_id.upper().index(marker) + len(marker):]
        items.append({
            "id": dev_id,
            "name": str(raw.get("name") or dev_id).strip(),
            "default": bool(default_id) and dev_id.lower() == default_id,
        })
    if not items:
        return [_default_device()]
    return _index_devices(items)


# --------------------------------------------------------- macOS 设备枚举 ---
def _mac_devices():
    exe = _which("SwitchAudioSource")
    if exe:
        code, out, _ = _run([exe, "-a", "-t", "output"])
        if code == 0 and out.strip():
            default = ""
            code2, out2, _ = _run([exe, "-c", "-t", "output"])
            if code2 == 0:
                default = out2.strip()
            items = []
            for line in out.splitlines():
                name = line.strip()
                if not name:
                    continue
                items.append({"id": name, "name": name, "default": name == default})
            return _index_devices(items)
    return _system_profiler_devices()


def _system_profiler_devices():
    exe = _which("system_profiler") or "/usr/sbin/system_profiler"
    code, out, _ = _run([exe, "SPAudioDataType"], timeout=30)
    if code != 0 or not out.strip():
        return [_default_device()]
    lines = out.splitlines()
    base_indent = None
    for line in lines:
        if line.strip() == "Devices:":
            base_indent = len(line) - len(line.lstrip())
            break
    if base_indent is None:
        return [_default_device()]
    dev_indent = base_indent + 4
    items = []
    current = None
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.endswith(":") and indent == dev_indent:
            if current:
                items.append(current)
            current = {"name": stripped[:-1], "text": ""}
            continue
        if current is not None:
            current["text"] += stripped + "\n"
    if current:
        items.append(current)
    devices = []
    for block in items:
        text = block["text"]
        if "Output Channels" not in text and "Output Source" not in text:
            continue
        devices.append({
            "id": block["name"],
            "name": block["name"],
            "default": "Default Output Device: Yes" in text,
        })
    return _index_devices(devices) if devices else [_default_device()]


# ------------------------------------------------------------------ 对外 API ---
if IS_WIN:
    _BACKENDS = (MpvBackend, FfplayBackend, ShellDefaultBackend)
elif IS_MAC:
    _BACKENDS = (MpvBackend, AfplayBackend)
else:
    _BACKENDS = (PulseBackend, MpvBackend, FfplayBackend)


def pick_backend():
    """按优先级找第一个「能真的枚举出设备」的后端（可用但跑不动就继续下一个）。"""
    errors = []
    for cls in _BACKENDS:
        try:
            if not cls.available():
                continue
            backend = cls()
            backend.devices()  # 探活：pactl 连不上之类的问题在这里暴露
            return backend
        except BackendError as exc:
            errors.append("%s: %s" % (cls.name, exc.message))
        except Exception as exc:  # noqa: BLE001 —— 单个后端探测失败不影响其它后端
            errors.append("%s: %s" % (cls.name, exc))
    if errors:
        raise BackendError(
            "没有可用的音频后端（%s）。Linux 上若是 PulseAudio/PipeWire 连接失败，"
            "通常是 XDG_RUNTIME_DIR / PULSE_SERVER 没指向 /run/user/<uid>/pulse/native"
            % "；".join(errors),
            code="no_backend",
        )
    raise BackendError(
        "未找到可用的音频后端（Linux: pactl+paplay 或 mpv/ffplay；macOS: mpv 或 afplay；"
        "Windows: mpv/ffplay）",
        code="no_backend",
    )


def list_devices():
    _cleanup_logs()
    backend = pick_backend()
    return backend, backend.devices()


def resolve_device(devices, value):
    """device 解析：空 → 默认设备；序号 / id / 名称片段 → 唯一设备。"""
    if value is None or value == "":
        for item in devices:
            if item.get("default"):
                return item
        return None
    if isinstance(value, bool):
        raise BackendError("device 不能是布尔值", code="invalid_argument")
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        index = int(text)
        if index < 0 or index >= len(devices):
            raise BackendError(
                "device 序号 %d 超出范围（0~%d）" % (index, len(devices) - 1),
                code="invalid_argument",
            )
        return devices[index]
    lowered = text.lower()
    for item in devices:
        if item["id"].lower() == lowered:
            return item
    hits = [i for i in devices if lowered in i["id"].lower() or lowered in i["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        names = "、".join("%d:%s" % (h["index"], h["name"]) for h in hits[:6])
        raise BackendError(
            "设备 '%s' 匹配到多个：%s；请改用序号" % (value, names),
            code="ambiguous_device",
        )
    raise BackendError(
        "没有匹配的设备 '%s'；先 action=list 查看可选设备" % value,
        code="device_not_found",
    )


def play_audio(path, device_value=None):
    backend, devices = list_devices()
    device = resolve_device(devices, device_value)
    proc = backend.play(path, device)
    return backend, device, proc
