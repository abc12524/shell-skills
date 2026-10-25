# skill: phone_thermal
# description: 读取手机各部位温度（电池 / 屏幕 / 主板 / CPU / GPU / 摄像头等，℃），按温度降序
# status: enable
# usage:
#   arguments.zone = 可选，按热区过滤：type 片段（cpu / battery / camera）
#   返回: count（有效热区数）/ groups（部位 + 组内最高温，降序，首项即最热）
#   示例: {}   {"zone": "cpu"}
"""phone_thermal —— 读取手机热区温度（仅 Android）。

扫描 /sys/class/thermal/thermal_zone*，过滤电压/电流/阈值等非温度节点，按部位聚合取最高温，
只返回部位级结果（不回传原始热区名与逐条读数）；非 Linux 内核平台返回 unsupported_platform，
读不到数据返回 io_error。
"""
import glob
import os
import sys

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

THERMAL = "/sys/class/thermal/thermal_zone*"

# 需要排除的非温度热区（电压/电流/电平/百分比/跳变阈值等）
SKIP_KEYS = ("bcl-lvl", "ibat-lvl", "trip", "vbat", "socd")
# 有效区间（单位 0.001℃），越界视为未接入或异常值
MIN_VALID = -10000  # -10℃
MAX_VALID = 200000  # 200℃

# 关键部位：关键字 -> 展示名（按顺序匹配，命中即归类）
KEY_GROUPS = [
    ("shell_front", "机身正面"),
    ("shell_back", "机身背面"),
    ("shell_frame", "中框"),
    ("oled", "屏幕"),
    ("board_ntc", "主板"),
    ("wlan", "Wi-Fi 模块"),
    ("battery", "电池"),
    ("ddr", "内存"),
    ("gpuss", "GPU"),
    ("mdmss", "基带"),
    ("camera", "摄像头"),
    ("cpullc", "CPU"),
    ("cpu-", "CPU"),
]


def _group_of(ztype):
    """热区 type -> 展示名；未归类返回 None"""
    for key, name in KEY_GROUPS:
        if key in ztype:
            return name
    return None


def _read_zones():
    """扫描 /sys/class/thermal，返回 [{'type':..., 'c': 摄氏度}, ...]"""
    zones = []
    for path in sorted(glob.glob(THERMAL)):
        try:
            with open(os.path.join(path, "type")) as f:
                ztype = f.read().strip()
            with open(os.path.join(path, "temp")) as f:
                raw = f.read().strip()
        except OSError:
            continue
        if not ztype or any(k in ztype for k in SKIP_KEYS):
            continue
        try:
            val = int(raw)
        except ValueError:
            continue
        if val < MIN_VALID or val > MAX_VALID:
            continue
        zones.append({"type": ztype, "c": round(val / 1000.0, 1)})
    return zones


def _unsupported():
    """适用范围仅 Android（Linux 内核）；Windows / macOS 等返回原因字符串"""
    if sys.platform.startswith("linux") or "android" in sys.platform.lower():
        return None
    return "本技能仅支持 Android，当前平台 %s 不受支持" % sys.platform


def run(arguments):
    reason = _unsupported()
    if reason:
        return error(reason, code="unsupported_platform")
    if not isinstance(arguments, dict):
        return error("arguments 必须是对象", code="invalid_argument")
    zone = arguments.get("zone")
    if zone is not None and not isinstance(zone, str):
        return error("zone 必须是字符串", code="invalid_argument")

    zones = _read_zones()
    if not zones:
        return error("未能读取任何热区数据（无权限或路径不存在）", code="io_error")

    if zone:
        kw = zone.strip().lower()
        zones = [z for z in zones if kw in z["type"].lower()]
        if not zones:
            return error("未找到匹配 %r 的热区" % zone, code="not_found")

    # 概览：每个关键部位取组内最高温
    groups = {}
    for z in zones:
        name = _group_of(z["type"])
        if name is None:
            continue
        cur = groups.get(name)
        if cur is None or z["c"] > cur:
            groups[name] = z["c"]

    groups_list = [{"name": n, "temp": round(t, 1)}
                   for n, t in sorted(groups.items(), key=lambda kv: -kv[1])]

    return ok({"count": len(zones), "groups": groups_list})
