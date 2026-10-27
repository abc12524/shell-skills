# skill: sensor
# description: 读取 Android 传感器：默认列出传感器名，按名称读实时值
# status: enable
# usage:
#   arguments.action = list（默认）| read | info
#     list  列「索引: 类型名(设备名)」；可选 key/type/wakeup 过滤，fields=[...] 或 detail=true 补元数据
#     read  按 key 读值，返回 索引/名称/value（读不到给 note 或 error）
#     info  运行环境：架构 / 库加载说明 / 结构体大小
#   arguments.key = 索引 | 类型号 | 名称·类型名·别名 | list 里的整串；read 默认 "all"
#   arguments.rate_us=100000（微秒）· max_wait=1.5s · max_total=30s（仅 read）
#   示例: {} / {"action":"list","wakeup":true} / {"action":"read","key":"加速度"}
#   示例: {"action":"list","type":[1,4],"fields":["vendor","resolution"]} / {"action":"read","key":0}
#   注意: 计步器/心率需宿主 App manifest 权限（未声明时 enable 返回 -22）；on_change/one_shot 类读不到值属正常
"""sensor —— 读取 Android 传感器（仅 Android）。

用 ctypes 调 libandroid.so 的 ASensorManager / ASensorEventQueue，枚举全部传感器并轮询实时值：
不依赖 phone_tool 白名单，只使用 API 9 起的 NDK 符号，ASensorEvent 按 104 字节布局解析。

可读性由宿主 App 的 manifest 决定（脚本无法申请权限）：普通运动/环境类无需权限；
计步器(19)/步数检测(18) 需 ACTIVITY_RECOGNITION、心率(21) 需 BODY_SENSORS，未声明时 enable 返回 -22。
on_change / one_shot 类传感器只在事件发生时上报，轮询读不到属正常；系统传感器开关关闭时枚举为 0。
部分机型 libandroid.so 经 libunwindstack 依赖 liblzma 的 CrcGenerateTable，按名加载会先失败，
此时自动预加载 /system/lib64/liblzma.so 重试，结果记在 info.load_note。
"""
import json
import os
import sys
import time

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

# 延迟导入的 ctypes（避免 import 时就往 stderr 刷 linker 告警）
ctypes = None
ASensorEvent = None

# Android 标准传感器类型 -> 中文名
TYPE_NAMES = {
    1: "加速度计", 2: "磁力计", 3: "方向", 4: "陀螺仪", 5: "光线",
    6: "气压", 7: "温度", 8: "距离", 9: "重力", 10: "线性加速度",
    11: "旋转向量", 12: "相对湿度", 13: "环境温度", 14: "磁力未校准",
    15: "游戏旋转向量", 16: "陀螺仪未校准", 17: "重要运动", 18: "步数检测",
    19: "计步器", 20: "地磁旋转向量", 21: "心率", 22: "倾斜检测",
    23: "唤醒手势", 24: "注视手势", 25: "拾起手势", 26: "腕部倾斜手势",
    27: "设备方向", 28: "6DOF姿态", 29: "静止检测", 30: "运动检测",
    31: "心跳", 32: "动态传感器元数据", 33: "附加信息", 34: "低延迟离体检测",
    35: "加速度未校准",
}

# 上报模式（ASensor_getReportingMode）：事件触发型通常读不到实时值
REPORTING_NAMES = {0: "continuous", 1: "on_change", 2: "one_shot", 3: "special"}

# list 可按需补充的字段（detail=true 等同全选）
LIST_FIELDS = ("type", "vendor", "min_delay_us", "resolution", "is_wakeup", "reporting_mode")

# 别名表：类型号 -> 可匹配的关键词（含中英文、phone_tool 的叫法）
ALIASES = {
    1: ["加速度计", "加速度", "accel", "accelerometer", "g-sensor", "gsensor"],
    2: ["磁力计", "磁力", "磁场", "指南针", "compass", "magnet"],
    3: ["方向传感器", "orientation", "azimuth"],
    4: ["陀螺仪", "陀螺", "角速度", "gyro"],
    5: ["光线", "环境光", "光照", "亮度", "光感", "lux", "light", "ambient"],
    6: ["气压", "气压计", "barometer", "pressure"],
    7: ["温度传感器", "temperature"],
    8: ["距离传感器", "距离", "接近", "proximity", "prox"],
    9: ["重力", "gravity"],
    10: ["线性加速度", "线性加速", "linear", "linear_acceleration"],
    11: ["旋转向量", "rotation vector", "rotvec"],
    12: ["相对湿度", "湿度", "humidity"],
    13: ["环境温度", "ambient temperature"],
    14: ["磁力未校准", "未校准磁", "uncalibrated magnet"],
    15: ["游戏旋转向量", "游戏旋转", "game rotation"],
    16: ["陀螺仪未校准", "未校准陀螺", "uncalibrated gyro"],
    17: ["重要运动", "significant motion"],
    18: ["步数检测", "走路检测", "step detect", "step_detect"],
    19: ["计步器", "计步", "步数", "pedometer", "step counter", "step_counter"],
    20: ["地磁旋转向量", "地磁旋转", "geomagnetic"],
    21: ["心率", "heart rate"],
    22: ["倾斜检测", "倾斜", "tilt"],
    23: ["唤醒手势", "wake gesture"],
    24: ["注视手势", "注视", "glance"],
    25: ["拾起手势", "拾起", "拿起", "pick up", "pick_up"],
    26: ["腕部倾斜", "wrist tilt"],
    27: ["设备方向", "device orient"],
    28: ["6dof", "姿态"],
    29: ["静止检测", "静止", "stationary"],
    30: ["运动检测", "motion detect", "motion_detect"],
    31: ["心跳", "heartbeat"],
    35: ["加速度未校准", "未校准加速度", "uncalibrated accelerometer"],
}

# 全局缓存（每次 skill 调用都是新进程，惰性初始化）
_lib = None
_mgr = None
_sensors = []
_meta = []
_arch = ""
_load_note = ""
_is_wakeup_fn = None      # ASensor_isWakeUpSensor（API 26+，缺失时用名称启发式）
_reporting_mode_fn = None  # ASensor_getReportingMode（API 26+）


class _StderrMute:
    """临时把 fd 2 指向 /dev/null，吞掉 linker 命名空间告警；退出时恢复。"""

    def __enter__(self):
        self._saved = None
        self._devnull = None
        try:
            sys.stderr.flush()
        except Exception:
            pass
        try:
            self._devnull = os.open(os.devnull, os.O_WRONLY)
            self._saved = os.dup(2)
            os.dup2(self._devnull, 2)
        except Exception:
            self._saved = None
        return self

    def __exit__(self, *exc):
        try:
            if self._saved is not None:
                os.dup2(self._saved, 2)
                os.close(self._saved)
        except Exception:
            pass
        if self._devnull is not None:
            try:
                os.close(self._devnull)
            except Exception:
                pass
        return False


def _is_android():
    """粗判 Android：存在 /system 分区（含 32/64 位库目录之一）。"""
    return os.path.isdir("/system") and (
        os.path.isdir("/system/lib64") or os.path.isdir("/system/lib"))


def _ensure_ctypes():
    """延迟导入 ctypes 并定义事件结构体（导入过程屏蔽 stderr）。"""
    global ctypes, ASensorEvent
    if ctypes is not None:
        return ctypes
    with _StderrMute():
        import ctypes as _c

        class _U(_c.Union):
            _fields_ = [("data", _c.c_float * 16), ("u64", _c.c_uint64 * 8)]

        class _Event(_c.Structure):
            _fields_ = [
                ("version", _c.c_int32),
                ("sensor", _c.c_int32),
                ("type", _c.c_int32),
                ("reserved0", _c.c_int32),
                ("timestamp", _c.c_int64),
                ("u", _U),
                ("flags", _c.c_uint32),
                ("reserved1", _c.c_int32 * 3),
            ]

    ctypes = _c
    ASensorEvent = _Event
    return ctypes


def _find_lib(name):
    """按常见路径查找系统 .so，兼容 32/64 位与不同分区。"""
    bases = (
        "/system/lib64", "/system/lib",
        "/apex/com.android.runtime/lib64", "/apex/com.android.runtime/lib",
        "/apex/com.android.art/lib64", "/apex/com.android.art/lib",
        "/vendor/lib64", "/vendor/lib",
        "/system_ext/lib64", "/system_ext/lib",
    )
    out, seen = [], set()
    for b in bases:
        p = os.path.join(b, name)
        if os.path.isfile(p) and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _load_libandroid():
    """加载 libandroid.so；必要时预加载 liblzma/libzstd 解决符号依赖。

    返回 (lib, note)：note 是给模型看的简短说明；失败时抛 OSError，且错误文本截断，
    避免把整段 linker 报错刷进工具结果。
    """
    _ensure_ctypes()
    errors, preloaded = [], []
    with _StderrMute():
        try:
            return ctypes.CDLL("libandroid.so", mode=ctypes.RTLD_GLOBAL), "直接加载成功"
        except OSError as exc:
            errors.append(str(exc))

        # 典型场景：libandroid -> libunwindstack -> 需要 liblzma 提供 CrcGenerateTable，
        # 而该依赖不在 Python 进程的命名空间里；先按绝对路径把依赖抬成 RTLD_GLOBAL 即可。
        for dep in ("liblzma.so", "libzstd.so"):
            for p in _find_lib(dep):
                try:
                    ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
                    preloaded.append(p)
                except OSError as exc:
                    errors.append(str(exc))

        for cand in ["libandroid.so"] + _find_lib("libandroid.so"):
            try:
                lib = ctypes.CDLL(cand, mode=ctypes.RTLD_GLOBAL)
                if preloaded:
                    return lib, "预加载 %s 后加载成功" % "、".join(preloaded)
                return lib, "重试后加载成功"
            except OSError as exc:
                errors.append(str(exc))
    msg = (errors[-1] if errors else "unknown error")[:300]
    raise OSError("libandroid.so 加载失败：%s" % msg)


def _setup_signatures(lib):
    lib.ASensorManager_getInstance.restype = ctypes.c_void_p
    lib.ASensorManager_getSensorList.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))]
    lib.ASensorManager_getSensorList.restype = ctypes.c_int
    lib.ASensor_getName.argtypes = [ctypes.c_void_p]
    lib.ASensor_getName.restype = ctypes.c_char_p
    lib.ASensor_getVendor.argtypes = [ctypes.c_void_p]
    lib.ASensor_getVendor.restype = ctypes.c_char_p
    lib.ASensor_getType.argtypes = [ctypes.c_void_p]
    lib.ASensor_getType.restype = ctypes.c_int
    lib.ASensor_getMinDelay.argtypes = [ctypes.c_void_p]
    lib.ASensor_getMinDelay.restype = ctypes.c_int
    lib.ASensor_getResolution.argtypes = [ctypes.c_void_p]
    lib.ASensor_getResolution.restype = ctypes.c_float

    lib.ALooper_prepare.argtypes = [ctypes.c_int]
    lib.ALooper_prepare.restype = ctypes.c_void_p
    lib.ASensorManager_createEventQueue.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    lib.ASensorManager_createEventQueue.restype = ctypes.c_void_p
    lib.ASensorEventQueue_enableSensor.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.ASensorEventQueue_enableSensor.restype = ctypes.c_int
    lib.ASensorEventQueue_setEventRate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32]
    lib.ASensorEventQueue_setEventRate.restype = ctypes.c_int
    lib.ASensorEventQueue_disableSensor.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.ASensorEventQueue_disableSensor.restype = ctypes.c_int
    lib.ASensorEventQueue_hasEvents.argtypes = [ctypes.c_void_p]
    lib.ASensorEventQueue_hasEvents.restype = ctypes.c_int
    lib.ASensorEventQueue_getEvents.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.ASensorEventQueue_getEvents.restype = ctypes.c_ssize_t
    lib.ASensorManager_destroyEventQueue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.ASensorManager_destroyEventQueue.restype = ctypes.c_int


def _setup_optional(lib):
    """API 26+ 才有的符号（唤醒型判定 / 上报模式）；缺失时退回名称启发式。"""
    global _is_wakeup_fn, _reporting_mode_fn
    for sym, slot in (("ASensor_isWakeUpSensor", "_is_wakeup_fn"),
                      ("ASensor_getReportingMode", "_reporting_mode_fn")):
        fn = getattr(lib, sym, None)
        if fn is not None:
            try:
                fn.argtypes = [ctypes.c_void_p]
                fn.restype = ctypes.c_int
            except Exception:
                fn = None
        if slot == "_is_wakeup_fn":
            _is_wakeup_fn = fn
        else:
            _reporting_mode_fn = fn


def _sensor_info(i, s):
    name = _lib.ASensor_getName(s)
    name = name.decode("utf-8", "replace") if name else ""
    vendor = _lib.ASensor_getVendor(s)
    vendor = vendor.decode("utf-8", "replace") if vendor else ""
    t = _lib.ASensor_getType(s)
    delay = _lib.ASensor_getMinDelay(s)
    try:
        res = round(float(_lib.ASensor_getResolution(s)), 6)
    except Exception:
        res = 0.0
    # 唤醒型优先用系统接口判定，拿不到再退回名称启发式
    is_wake = "wakeup" in name.lower()
    if _is_wakeup_fn is not None:
        try:
            is_wake = _is_wakeup_fn(s) != 0
        except Exception:
            pass
    info = {
        "index": i,
        "type": t,
        "type_name": TYPE_NAMES.get(t, "自定义(%d)" % t),
        "name": name,
        "vendor": vendor,
        "min_delay_us": delay,
        "resolution": res,
        "is_wakeup": is_wake,
    }
    if _reporting_mode_fn is not None:
        try:
            mode = _reporting_mode_fn(s)
            info["reporting_mode"] = REPORTING_NAMES.get(mode, mode)
        except Exception:
            pass
    return info


def _label(m):
    """给人看的一行标识：类型名(设备名)，可直接当 read 的 key 回传。"""
    return "%s(%s)" % (m["type_name"], m["name"]) if m["name"] else m["type_name"]


def _init():
    global _lib, _mgr, _sensors, _meta, _arch, _load_note
    if _lib is not None:
        return
    _ensure_ctypes()
    _arch = "64位" if ctypes.sizeof(ctypes.c_void_p) == 8 else "32位"
    lib, _load_note = _load_libandroid()
    _lib = lib
    _setup_signatures(lib)
    _setup_optional(lib)
    _mgr = lib.ASensorManager_getInstance()
    if not _mgr:
        raise RuntimeError("ASensorManager_getInstance() 返回空指针")
    lst = ctypes.POINTER(ctypes.c_void_p)()
    n = lib.ASensorManager_getSensorList(_mgr, ctypes.byref(lst))
    if n <= 0:
        raise RuntimeError(
            "未获取到任何传感器（getSensorList 返回 %d）："
            "请检查系统「传感器」开关是否被关闭（快捷设置里的 Sensors Off / 开发者选项）" % n)
    _sensors = [lst[i] for i in range(n)]
    _meta = [_sensor_info(i, s) for i, s in enumerate(_sensors)]


def _alias_types(low):
    """把关键词映射成类型号集合；取最长匹配，避免「加速度」误命中未校准等。"""
    best, types = 0, set()
    for t, names in ALIASES.items():
        for n in names:
            n = n.lower()
            if n and n in low:
                if len(n) > best:
                    best, types = len(n), {t}
                elif len(n) == best:
                    types.add(t)
    return types


def _select(key):
    if key is None or str(key).strip() == "" or str(key).strip().lower() == "all":
        return list(range(len(_sensors)))
    s = str(key).strip()
    # 允许把 list 里的 "类型名(设备名)" 整串回传，拆开分别匹配
    cands = [s]
    for lb, rb in (("(", ")"), ("（", "）")):
        if lb in s and s.endswith(rb):
            i = s.index(lb)
            cands = [s[:i].strip(), s[i + 1:-1].strip(), s]
            break
    for cand in cands:
        if not cand:
            continue
        if cand.isdigit():
            n = int(cand)
            if 0 <= n < len(_sensors):
                return [n]
            hits = [i for i, m in enumerate(_meta) if m["type"] == n]
            if hits:
                return hits
            continue  # 纯数字既不是索引也不是类型号：不要再拿数字去做子串匹配
        low = cand.lower()
        # 1) 直接子串匹配：名称 / 类型名 / 厂商
        hits = [i for i, m in enumerate(_meta)
                if low in m["name"].lower() or low in m["type_name"].lower() or low in m["vendor"].lower()]
        if hits:
            return hits
        # 2) 别名匹配（环境光/亮度/lux -> 光线，指南针 -> 磁力计 …）
        types = _alias_types(low)
        if types:
            hits = [i for i, m in enumerate(_meta) if m["type"] in types]
            if hits:
                return hits
    return None


def _fmt_value(ev):
    t = ev.type
    d = [round(float(x), 4) for x in ev.u.data[:6]]
    if t == 1:
        return "x=%.4f y=%.4f z=%.4f m/s²" % (d[0], d[1], d[2])
    if t == 2:
        return "x=%.4f y=%.4f z=%.4f μT" % (d[0], d[1], d[2])
    if t == 4:
        return "x=%.4f y=%.4f z=%.4f rad/s" % (d[0], d[1], d[2])
    if t == 5:
        return "lux≈%.4f（原始 data=%s）" % (d[0], d[:6])
    if t == 6:
        return "%.4f hPa" % d[0]
    if t == 8:
        return "%.4f cm" % d[0]
    if t in (9, 10):
        return "x=%.4f y=%.4f z=%.4f m/s²" % (d[0], d[1], d[2])
    if t in (11, 15, 20):
        return "四元数 w=%.4f x=%.4f y=%.4f z=%.4f" % (d[3], d[0], d[1], d[2])
    if t in (14, 16):
        return "x=%.4f y=%.4f z=%.4f（未校准）" % (d[0], d[1], d[2])
    if t == 19:
        return "累计步数=%d" % ev.u.u64[0]
    if t == 18:
        return "检测到一步（data0=%.4f）" % d[0]
    if t == 3:
        return "azimuth=%.4f pitch=%.4f roll=%.4f" % (d[0], d[1], d[2])
    if t == 27:
        return "方向=%d" % int(d[0])
    return "data=%s" % d


def _enable_note(code):
    if code == -22:
        return "enable 返回 -22(EINVAL)：该传感器可能需要额外权限（计步器/步数检测需 ACTIVITY_RECOGNITION）或设备不支持"
    if code != 0:
        return "enable 返回 %d" % code
    return ""


def _read_one(s, rate_us, max_wait):
    looper = _lib.ALooper_prepare(1)
    q = _lib.ASensorManager_createEventQueue(_mgr, looper, -1, None, None)
    if not q:
        return {"enabled": False, "events": 0, "value": "", "note": "事件队列创建失败"}
    r1 = _lib.ASensorEventQueue_enableSensor(q, s)
    if r1 != 0:
        # 权限不足 / 设备不支持：等下去也收不到事件，直接返回
        _lib.ASensorManager_destroyEventQueue(_mgr, q)
        return {"enabled": False, "enable_code": r1, "events": 0, "value": "",
                "note": _enable_note(r1) or "enableSensor 返回 %d" % r1}
    r2 = _lib.ASensorEventQueue_setEventRate(q, s, rate_us)
    ev = ASensorEvent()
    cnt = 0
    t0 = time.time()
    while time.time() - t0 < max_wait:
        # 先查有无事件再读，避免个别机型上 getEvents 阻塞
        if _lib.ASensorEventQueue_hasEvents(q) > 0:
            cnt = _lib.ASensorEventQueue_getEvents(q, ctypes.byref(ev), 1)
            if cnt > 0:
                break
        time.sleep(0.01)
    out = {
        "enabled": True,
        "enable_code": r1,
        "set_rate_code": r2,
        "events": cnt if cnt > 0 else 0,
    }
    if cnt > 0:
        out["value"] = _fmt_value(ev)
    else:
        out["value"] = ""
        out["note"] = "无事件（可能为事件触发型传感器，或采样窗口内未上报）"
    _lib.ASensorEventQueue_disableSensor(q, s)
    _lib.ASensorManager_destroyEventQueue(_mgr, q)
    return out


def _to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_bool(v):
    """宽松布尔：true/1/"true"/"yes"/"on" 为真，其余为假。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


def _list_payload(a):
    """list 动作：默认只给「索引 -> 类型名(设备名)」，可按条件过滤或补字段。"""
    idxs = list(range(len(_sensors)))
    filtered = False

    key = a.get("key")
    if key is not None and str(key).strip().lower() not in ("", "all"):
        hit = _select(key)
        if not hit:
            return error("未匹配到传感器 '%s'，可先不带 key 列出清单" % key, code="not_found")
        idxs, filtered = hit, True

    types = a.get("type")
    if types is not None:
        want = set()
        for t in (types if isinstance(types, (list, tuple)) else [types]):
            n = _to_int(t, None)
            if n is not None:
                want.add(n)
        if want:
            idxs = [i for i in idxs if _meta[i]["type"] in want]
            filtered = True

    wake = a.get("wakeup")
    if wake is not None:
        flag = _to_bool(wake)
        idxs = [i for i in idxs if bool(_meta[i].get("is_wakeup")) == flag]
        filtered = True

    fields = a.get("fields")
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.replace("，", ",").split(",") if f.strip()]
    want_fields = [f for f in (fields or []) if f in LIST_FIELDS]
    unknown = [f for f in (fields or []) if f not in LIST_FIELDS]
    if unknown:
        return error("未知字段 %s，可用: %s（或 detail=true 全要）"
                     % ("、".join(unknown), " / ".join(LIST_FIELDS)),
                     code="invalid_argument")
    if _to_bool(a.get("detail")):
        want_fields = list(LIST_FIELDS)

    if want_fields:
        rows = []
        for i in idxs:
            m = _meta[i]
            row = {"index": i, "name": _label(m)}
            for f in want_fields:
                if f in m:
                    row[f] = m[f]
            rows.append(row)
        payload = {"sensor_count": len(_sensors), "sensors": rows}
    else:
        payload = {"sensor_count": len(_sensors),
                   "sensors": {str(i): _label(_meta[i]) for i in idxs}}
    if filtered:
        payload["matched"] = len(idxs)
        if not idxs:
            payload["note"] = "没有符合条件（key / type / wakeup）的传感器"
    return ok(payload)


def run(arguments):
    a = arguments if isinstance(arguments, dict) else {}
    action = str(a.get("action") or "list").strip().lower()
    if action not in ("list", "read", "info"):
        return error("未知 action '%s'，可用: list / read / info" % action, code="unknown_action")

    if not _is_android():
        return error("仅支持 Android（未检测到 /system 分区与 libandroid.so 运行环境）",
                     code="unsupported_platform")

    try:
        _init()
    except Exception as exc:
        return error("传感器库初始化失败: %s" % exc, code="internal")

    if action == "info":
        return ok({
            "arch": _arch,
            "load_note": _load_note,
            "sensor_count": len(_sensors),
            "sizeof_ASensorEvent": ctypes.sizeof(ASensorEvent),
            "has_wakeup_api": _is_wakeup_fn is not None,
            "has_reporting_mode_api": _reporting_mode_fn is not None,
        })

    if action == "list":
        return _list_payload(a)

    # read
    key = a.get("key", "all")
    rate_us = _to_int(a.get("rate_us", 100000), 100000)
    if rate_us < 0:
        rate_us = 0
    max_wait = min(max(_to_float(a.get("max_wait", 1.5), 1.5), 0.05), 10.0)
    max_total = min(max(_to_float(a.get("max_total", 30), 30), 1.0), 60.0)
    idxs = _select(key)
    if not idxs:
        return error("未匹配到传感器 '%s'，可先用 action=list 查看" % key, code="not_found")

    notes = []
    if 0 < rate_us < 5000:
        notes.append("请求采样率高于 200Hz：Android 12+ 对未声明 HIGH_SAMPLING_RATE_SENSORS 的应用会限速 200Hz")
    deadline = time.time() + max_total
    results, truncated = [], False
    for n, i in enumerate(idxs):
        left = deadline - time.time()
        if left <= 0:
            truncated = True
            break
        # 目标多时按剩余名额分摊等待，保证每个传感器都有机会读到一帧
        wait = min(max_wait, max(0.05, left / max(1, len(idxs) - n)))
        m = _meta[i]
        r = _read_one(_sensors[i], rate_us, wait)
        item = {"index": i, "name": _label(m)}
        if r.get("events", 0) > 0:
            item["value"] = r.get("value", "")
        elif r.get("enabled"):
            item["note"] = r.get("note") or "采样窗口内无事件"
        else:
            item["error"] = r.get("note") or "enableSensor 失败（%s）" % r.get("enable_code")
        results.append(item)

    payload = {"read_count": len(results), "sensors": results}
    if truncated:
        payload["truncated"] = True
        notes.append("总预算 %.0fs 用尽，仍有 %d 个传感器未读：可用 key 指定目标，或调大 max_total"
                     % (max_total, len(idxs) - len(results)))
    if notes:
        payload["notes"] = notes
    return ok(payload)


if __name__ == "__main__":  # 本地调试：python skill.py '{"action":"list"}'
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
