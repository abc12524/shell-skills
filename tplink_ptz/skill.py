# skill: tplink_ptz
# description: TP-Link 摄像头 PTZ 云台控制（列表 / 状态 / 方向转动 / 定位 / 预置点）
# status: enable
# usage:
#   arguments.action = list | info | status | left | right | up | down | stop |
#                      center | relative | absolute | presets | goto | save | delete
#     list                    列出已配置摄像头（无需 camera）
#     info                    读取 PTZ 能力（pan/tilt 范围、速度上限、预置点上限）
#     status                  读取当前 pan/tilt 位置与运动状态
#     left|right|up|down      方向连续转动 arguments.seconds 秒后自动停止
#     stop                    立即停止（pan/tilt/zoom 全停）
#     center                  归中到 (0,0)
#     relative                相对位移，读 arguments.pan / tilt / zoom
#     absolute                绝对定位，读 arguments.pan / tilt / zoom（默认 0）
#     presets                 列出已保存预置点
#     goto                    转到预置点，需 arguments.id
#     save                    保存当前位姿为预置点，需 arguments.name
#     delete                  删除预置点，需 arguments.id
#   arguments.camera = 摄像头标识：节名(camera1) / 别名(前院) / IP（list 可省略）
#   arguments.speed  = 速度 0~1（默认读配置 default_speed=0.6）
#   arguments.seconds= 方向转动时长秒（默认读配置 default_duration=2.0）
#   arguments.pan / tilt / zoom = 位置或位移 -1~1（relative / absolute）
#   arguments.id     = 预置点编号（goto / delete）
#   arguments.name   = 预置点名称（save）
#   arguments.config = 可选，配置文件路径（默认同目录 tplink.conf）
#   示例: {"action": "list"}
#   示例: {"action": "right", "camera": "前院", "speed": 0.6, "seconds": 2}
#   示例: {"action": "goto", "camera": "camera1", "id": 3}
import configparser
import os
import time

import requests

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

TP_LINK_KEY = "RDpbLfCPsJZ7fiv"
TP_LINK_CHARSET = (
    "yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD02KZciXTysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57fQMc8L6aLgMLwygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3sfQ1xtXcPcf1aT303wAQhv66qzW"
)

ERROR_NAMES = {
    -40106: "EINVINSTRUCT(指令无效)",
    -40107: "EFORBID(禁止)",
    -40201: "EOVERFLOW(溢出)",
    -64301: "PTZ_ERR_UNKNOWN",
    -64302: "PTZ_ERR_ARGUMENT_INVALID(参数无效)",
    -64304: "PTZ_ERR_MOTOR_BLOCKED(电机被挡)",
    -64305: "PTZ_ERR_SET_PRESET_WHEN_MOTOR_MOVING",
    -64312: "PTZ_ERR_HOME_NOT_SUPPORTED(不支持归位)",
    -64314: "PTZ_ERR_COORD_OUT_OF_BOUNDS_FOR_ABSOLUTE_MOVE",
    -64315: "PTZ_ERR_COORD_OUT_OF_BOUNDS_FOR_RELATIVE_MOVE",
    -64316: "PTZ_ERR_SPEED_OUT_OF_BOUNDS(速度越界)",
    -64323: "PTZ_ERR_OUT_OF_LIMIT(超出限位)",
}

_VALID_ACTIONS = ("info", "status", "left", "right", "up", "down", "stop",
                  "center", "relative", "absolute", "presets", "goto", "save", "delete")


def _default_config():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tplink.conf")


def security_encode(password, key=TP_LINK_KEY, charset=TP_LINK_CHARSET):
    """TP-Link 密码加密算法"""
    result = ""
    pass_len, key_len, charset_len = len(password), len(key), len(charset)
    for i in range(max(pass_len, key_len)):
        pass_char = key_char = 187
        if i >= pass_len:
            key_char = ord(key[i])
        elif i >= key_len:
            pass_char = ord(password[i])
        else:
            pass_char = ord(password[i])
            key_char = ord(key[i])
        result += charset[(pass_char ^ key_char) % charset_len]
    return result


def _s(value):
    """PTZ 数值 -> 字符串（必须为字符串，否则 -64302）"""
    return f"{float(value):.6f}"


class TplinkCamera:
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    def __init__(self, section, name, ip, port, username, password, timeout=8):
        self.section = section
        self.name = name
        self.ip = ip
        self.port = int(port)
        self.username = username
        self.password = password
        self.timeout = float(timeout)
        self.stok = None

    @property
    def base(self):
        return f"http://{self.ip}" if self.port in (0, 80) else f"http://{self.ip}:{self.port}"

    def login(self):
        headers = dict(self.HEADERS, Referer=self.base)
        body = {"method": "do",
                "login": {"username": self.username, "password": security_encode(self.password)}}
        r = requests.post(self.base + "/", json=body, headers=headers, timeout=self.timeout)
        data = r.json()
        if "stok" not in data:
            raise RuntimeError(f"登录失败: {data}")
        self.stok = data["stok"]
        return self.stok

    def action(self, payload):
        if self.stok is None:
            self.login()
        headers = dict(self.HEADERS, Referer=self.base)
        url = f"{self.base}/stok={self.stok}/ds"
        r = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
        try:
            return r.json()
        except ValueError:
            return {"error_code": None, "raw": r.text}

    @staticmethod
    def _check(res, what):
        code = res.get("error_code") if isinstance(res, dict) else None
        if code != 0:
            hint = ERROR_NAMES.get(code, "")
            raise RuntimeError(f"{what} 失败: error_code={code} {hint} {res}")
        return res

    def get_capability(self):
        return self._check(self.action({"method": "get", "ptz": {"name": ["capability"]}}), "读取能力")

    def continuous_move(self, pan=0.0, tilt=0.0, zoom=0.0):
        return self._check(self.action({"method": "do", "ptz": {"continuous_move": {
            "velocity_pan": _s(pan), "velocity_tilt": _s(tilt), "velocity_zoom": _s(zoom)}}}), "连续转动")

    def stop(self):
        return self._check(self.action({"method": "do", "ptz": {"stop": {
            "pan": "1", "tilt": "1", "zoom": "1"}}}), "停止")

    def relative_move(self, pan=0.0, tilt=0.0, zoom=0.0):
        return self._check(self.action({"method": "do", "ptz": {"relative_move": {
            "translation_pan": _s(pan), "translation_tilt": _s(tilt), "translation_zoom": _s(zoom)}}}), "相对转动")

    def absolute_move(self, pan=0.0, tilt=0.0, zoom=0.0, speed=0.5):
        return self._check(self.action({"method": "do", "ptz": {"absolute_move": {
            "position_pan": _s(pan), "position_tilt": _s(tilt), "position_zoom": _s(zoom),
            "speed_pan": _s(speed), "speed_tilt": _s(speed), "speed_zoom": _s(speed)}}}), "绝对定位")

    def get_status(self):
        """当前 pan/tilt 位置与运动状态（不返回 zoom）"""
        res = self._check(self.action({"method": "do", "ptz": {"get_ptz_status": {}}}), "读取状态")
        return res.get("ptz", {}).get("status", {})

    def list_presets(self):
        """列出已保存的预置点"""
        res = self._check(self.action({"method": "get", "preset": {"name": ["preset"]}}), "读取预置点")
        return res.get("preset", {}).get("preset", {})

    def goto_preset(self, pid):
        return self._check(self.action({"method": "do", "preset": {
            "goto_preset": {"id": str(pid)}}}), "转到预置点")

    def set_preset(self, name):
        return self._check(self.action({"method": "do", "preset": {
            "set_preset": {"name": name, "save_ptz": "1"}}}), "保存预置点")

    def remove_preset(self, pid):
        return self._check(self.action({"method": "do", "preset": {
            "remove_preset": {"id": str(pid)}}}), "删除预置点")

    def move_dir(self, direction, speed, seconds):
        """方向连续转动，到时自动停止"""
        pan = tilt = 0.0
        if direction == "left":
            pan = -abs(speed)
        elif direction == "right":
            pan = abs(speed)
        elif direction == "up":
            tilt = abs(speed)
        elif direction == "down":
            tilt = -abs(speed)
        self.continuous_move(pan, tilt, 0.0)
        try:
            time.sleep(max(0.0, seconds))
        finally:
            self.stop()


def load_cameras(config_file):
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"配置文件不存在: {config_file}")
    cp = configparser.ConfigParser()
    cp.read(config_file, encoding="utf-8")

    timeout = cp.getfloat("general", "timeout", fallback=8)

    cameras = {}
    for section in cp.sections():
        if section == "general":
            continue
        cameras[section] = TplinkCamera(
            section=section,
            name=cp.get(section, "name", fallback=section),
            ip=cp.get(section, "ip").strip(),
            port=cp.getint(section, "port", fallback=80),
            username=cp.get(section, "username", fallback="admin"),
            password=cp.get(section, "password", fallback=""),
            timeout=timeout,
        )
    return cameras


def resolve_camera(cameras, key):
    if key is None:
        raise KeyError("缺少 camera（节名/别名/IP）")
    if key in cameras:
        return cameras[key]
    for cam in cameras.values():
        if key == cam.ip or key == cam.name:
            return cam
    # 支持纯数字，如 3 -> camera3
    if key.isdigit() and f"camera{key}" in cameras:
        return cameras[f"camera{key}"]
    raise KeyError(f"未找到摄像头: {key}")


def run(arguments):
    a = arguments or {}
    action = (a.get("action") or "").strip()

    if action == "list":
        try:
            cameras = load_cameras(a.get("config") or _default_config())
        except Exception as e:
            return error(str(e), code="config_error")
        return ok({
            "count": len(cameras),
            "cameras": [
                {"section": sec, "name": cam.name, "ip": cam.ip, "port": cam.port}
                for sec, cam in cameras.items()
            ],
        })

    if not action:
        return error("缺少 action", code="missing_argument")
    if action not in _VALID_ACTIONS:
        return error(f"未知 action '{action}'", code="unknown_action")

    try:
        cp = configparser.ConfigParser()
        cp.read(a.get("config") or _default_config(), encoding="utf-8")
        default_speed = cp.getfloat("general", "default_speed", fallback=0.6) if cp.has_section("general") else 0.6
        default_seconds = cp.getfloat("general", "default_duration", fallback=2.0) if cp.has_section("general") else 2.0
        cameras = load_cameras(a.get("config") or _default_config())
    except Exception as e:
        return error(str(e), code="config_error")

    try:
        cam = resolve_camera(cameras, a.get("camera"))
    except KeyError as e:
        return error(str(e), code="unknown_camera")

    speed = a.get("speed")
    speed = default_speed if speed is None else float(speed)
    seconds = a.get("seconds")
    seconds = default_seconds if seconds is None else float(seconds)
    pan = float(a.get("pan") or 0.0)
    tilt = float(a.get("tilt") or 0.0)
    zoom = float(a.get("zoom") or 0.0)

    try:
        if action == "info":
            cap = cam.get_capability().get("ptz", {}).get("capability", {})
            return ok({
                "camera": cam.section, "name": cam.name, "ip": cam.ip,
                "position_pan_range": cap.get("position_pan_range"),
                "position_tilt_range": cap.get("position_tilt_range"),
                "speed_pan_max": cap.get("speed_pan_max"),
                "speed_tilt_max": cap.get("speed_tilt_max"),
                "speed_zoom_max": cap.get("speed_zoom_max"),
                "continuous_move_supported": cap.get("continuous_move_supported"),
                "relative_move_supported": cap.get("relative_move_supported"),
                "absolute_move_supported": cap.get("absolute_move_supported"),
                "preset_number_max": cap.get("preset_number_max"),
            })
        if action == "status":
            st = cam.get_status()
            return ok({
                "camera": cam.section, "name": cam.name,
                "position_pan": st.get("position_pan"), "status_pan": st.get("status_pan"),
                "position_tilt": st.get("position_tilt"), "status_tilt": st.get("status_tilt"),
            })
        if action in ("left", "right", "up", "down"):
            cam.move_dir(action, speed, seconds)
            return ok({"camera": cam.section, "action": action, "speed": speed, "seconds": seconds})
        if action == "stop":
            cam.stop()
            return ok({"camera": cam.section, "action": "stop"})
        if action == "center":
            cam.absolute_move(0.0, 0.0, 0.0, speed)
            return ok({"camera": cam.section, "action": "center", "pan": 0.0, "tilt": 0.0, "speed": speed})
        if action == "relative":
            cam.relative_move(pan, tilt, zoom)
            return ok({"camera": cam.section, "action": "relative", "pan": pan, "tilt": tilt, "zoom": zoom})
        if action == "absolute":
            cam.absolute_move(pan, tilt, zoom, speed)
            return ok({"camera": cam.section, "action": "absolute", "pan": pan, "tilt": tilt,
                       "zoom": zoom, "speed": speed})
        if action == "presets":
            data = cam.list_presets()
            ids = data.get("id", [])
            names = data.get("name", [])
            pans = data.get("position_pan", [])
            tilts = data.get("position_tilt", [])
            zooms = data.get("position_zoom", [])
            presets = []
            for i, pid in enumerate(ids):
                presets.append({
                    "id": pid,
                    "name": names[i] if i < len(names) else "",
                    "pan": pans[i] if i < len(pans) else "",
                    "tilt": tilts[i] if i < len(tilts) else "",
                    "zoom": zooms[i] if i < len(zooms) else "",
                })
            return ok({"camera": cam.section, "name": cam.name, "count": len(presets), "presets": presets})
        if action == "goto":
            pid = a.get("id")
            if pid is None:
                return error("goto 需要 arguments.id", code="missing_argument")
            cam.goto_preset(pid)
            return ok({"camera": cam.section, "action": "goto", "id": pid})
        if action == "save":
            name = a.get("name")
            if not name:
                return error("save 需要 arguments.name", code="missing_argument")
            cam.set_preset(name)
            return ok({"camera": cam.section, "action": "save", "name": name})
        if action == "delete":
            pid = a.get("id")
            if pid is None:
                return error("delete 需要 arguments.id", code="missing_argument")
            cam.remove_preset(pid)
            return ok({"camera": cam.section, "action": "delete", "id": pid})
    except requests.exceptions.RequestException as e:
        return error(f"网络错误: {e}", code="network_error")
    except RuntimeError as e:
        return error(str(e), code="device_error")

    return error(f"未知 action '{action}'", code="unknown_action")


def _cli():
    import argparse
    import json

    p = argparse.ArgumentParser(description="TP-Link 摄像头 PTZ 云台控制")
    p.add_argument("-c", "--config", default=None, help="配置文件路径 (默认: 同目录 tplink.conf)")
    p.add_argument("camera", nargs="?", help="摄像头(节名/别名/IP)，或 list")
    p.add_argument("action", nargs="?", default="list")
    p.add_argument("--speed", type=float, default=None)
    p.add_argument("--seconds", type=float, default=None)
    p.add_argument("--pan", type=float, default=0.0)
    p.add_argument("--tilt", type=float, default=0.0)
    p.add_argument("--zoom", type=float, default=0.0)
    p.add_argument("--id", default=None)
    p.add_argument("--name", default=None)
    args = p.parse_args()

    arguments = {
        "action": args.action, "camera": args.camera, "config": args.config,
        "speed": args.speed, "seconds": args.seconds, "pan": args.pan,
        "tilt": args.tilt, "zoom": args.zoom, "id": args.id, "name": args.name,
    }
    arguments = {k: v for k, v in arguments.items() if v is not None}
    if args.camera == "list":
        arguments["action"] = "list"
    print(run(arguments))


if __name__ == "__main__":
    _cli()
