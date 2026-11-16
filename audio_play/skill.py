# skill: audio_play
# description: 音频播放（Linux / Windows / macOS）：列出输出设备、按设备播放音频文件，启动即返回不跟踪进度
# status: enable
# usage:
#   arguments.action = list（默认，列出输出设备）| play（播放）
#   arguments.device = 设备序号（list 的 index）、设备 id 或名称片段；play 省略则用系统默认设备
#   arguments.path   = 音频文件路径（play 必填，需为本机已存在的文件）
#   后端: 系统原生优先（pactl+paplay / afplay / mpv / ffplay）；指定非默认设备需要支持设备选择的后端
#   示例: {"action":"list"} / {"action":"play","path":"D:/music/a.mp3","device":1}
"""音频播放 skill：list 枚举输出设备，play 按设备播放并立即返回。

设计要点：
  * 只负责「启动播放」，watch 约 0.4s 确认没有立刻失败，不监控播放进度、不阻塞；
  * 后端按平台就近选择（见 _impl.py 顶部），list 与 play 用同一后端，index / id 一一对应；
  * 参数错误按 skill 规范处理：能默认就默认（省略 device 走默认设备），不能默认（缺 path）返回错误；
  * 相似参数名（file / file_path / audio、dev / output / sink 等）在 run() 入口归一化，不进 usage。
"""
import os

from ._impl import BackendError, list_devices, play_audio

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

ACTIONS = ("list", "play")
ACTION_ALIASES = {
    "ls": "list", "devices": "list", "device": "list", "show": "list",
    "start": "play", "open": "play", "run": "play",
}
PATH_KEYS = ("path", "file", "file_path", "audio", "src", "source")
DEVICE_KEYS = ("device", "dev", "output", "sink", "target")


def _first(arguments, keys):
    for key in keys:
        value = arguments.get(key)
        if value is not None and value != "":
            return value
    return None


def run(arguments):
    a = arguments or {}
    action = str(a.get("action") or "list").strip().lower()
    action = ACTION_ALIASES.get(action, action)

    try:
        if action == "list":
            backend, devices = list_devices()
            result = {
                "backend": backend.name,
                "can_select": backend.can_select,
                "count": len(devices),
                "default": next((d["id"] for d in devices if d.get("default")), ""),
                "devices": devices,
            }
            if not backend.can_select:
                result["hint"] = (
                    "当前后端 %s 只能输出到系统默认设备；要指定其它设备请安装 mpv"
                    "（mpv 可用 wasapi/{GUID}、coreaudio/…、pulse/… 精确选设备）" % backend.name
                )
            return ok(result)

        if action == "play":
            path = _first(a, PATH_KEYS)
            if not isinstance(path, str) or not path.strip():
                return error("play 需要 arguments.path（音频文件路径）", code="invalid_argument")
            path = os.path.abspath(os.path.expanduser(path.strip()))
            if not os.path.isfile(path):
                return error("音频文件不存在: %s" % path, code="not_found")
            backend, device, proc = play_audio(path, _first(a, DEVICE_KEYS))
            return ok({
                "backend": backend.name,
                "path": path,
                "device": None if device is None else {
                    "index": device["index"],
                    "id": device["id"],
                    "name": device["name"],
                    "default": bool(device.get("default")),
                },
                "pid": getattr(proc, "pid", None),
                "message": "已开始播放" if proc is not None else "已交给系统默认播放器",
            })

        return error("未知 action '%s'；可用: %s" % (action, " / ".join(ACTIONS)),
                     code="unknown_action")
    except BackendError as exc:
        return error(exc.message, code=exc.code)
    except Exception as exc:  # noqa: BLE001 —— 分发器兜底前先给出可读错误
        return error("audio_play 执行失败: %s" % exc, code="internal")
