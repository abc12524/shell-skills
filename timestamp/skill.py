# skill: timestamp
# description: 时间戳转换（当前时间 / 时间戳转日期 / 日期转时间戳，支持时区与自定义格式）
# status: enable
# usage:
#   arguments.action = now | to_date | to_ts
#     now      返回当前时间（时间戳 + 本地/UTC 日期）
#     to_date  时间戳 -> 日期字符串（需 value，可选 unit / tz / format）
#     to_ts    日期字符串 -> 时间戳（需 value，可选 tz / format）
#   now 参数：
#     tz       可选，时区偏移，如 "+08:00" / "-05:00" / "UTC"（默认本地时区）
#     format   可选，strftime 输出格式（默认 "%Y-%m-%d %H:%M:%S"）
#   to_date 参数：
#     value    时间戳，可为秒/毫秒/微秒（自动按位数判断），或数字字符串
#     unit     可选，强制单位：s | ms | us（不传则自动判断）
#     tz       可选，时区偏移，如 "+08:00" / "UTC"（默认本地时区）
#     format   可选，strftime 输出格式（默认 "%Y-%m-%d %H:%M:%S"）
#   to_ts 参数：
#     value    日期字符串，如 "2026-10-18 15:30:00"
#     tz       可选，value 所处时区偏移，如 "+08:00" / "UTC"（默认本地时区）
#     format   可选，输入格式（默认依次尝试常见格式）
#     unit     可选，输出单位：s（默认）| ms | us
#   示例: {"action": "now"}
#         {"action": "now", "tz": "+08:00", "format": "%Y-%m-%d %H:%M:%S"}
#         {"action": "to_date", "value": 1729234200}
#         {"action": "to_date", "value": 1729234200000, "tz": "UTC"}
#         {"action": "to_ts", "value": "2026-10-18 15:30:00", "tz": "+08:00"}
from datetime import datetime, timezone, timedelta

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

_DEFAULT_FMT = "%Y-%m-%d %H:%M:%S"
_INPUT_FMTS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


def _parse_tz(text):
    """把 tz 字符串解析成 tzinfo。空/None -> 本地时区；'UTC'/'Z' -> UTC；'+08:00' -> 固定偏移。"""
    if text is None or str(text).strip() == "":
        return None  # 交给 datetime 使用本地时区
    t = str(text).strip()
    up = t.upper()
    if up in ("UTC", "Z", "GMT", "UTC+0", "UTC+00:00", "+00:00"):
        return timezone.utc
    sign = 1
    body = t
    if t[0] in "+-":
        sign = -1 if t[0] == "-" else 1
        body = t[1:]
    if ":" in body:
        hh, mm = body.split(":", 1)
    else:
        hh, mm = body, "0"
    try:
        hours = int(hh)
        minutes = int(mm)
    except ValueError:
        raise ValueError(f"无法解析时区 '{text}'（示例: '+08:00' / '-05:00' / 'UTC'）")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _tzinfo_or_local(tz_obj):
    return tz_obj if tz_obj is not None else datetime.now().astimezone().tzinfo


def _detect_unit(value, explicit):
    if explicit:
        u = str(explicit).strip().lower()
        if u not in ("s", "ms", "us"):
            raise ValueError(f"unit 必须是 s / ms / us，收到: {explicit!r}")
        return u, 1.0
    n = abs(value)
    if n >= 1e15:  # 微秒量级
        return "us", 1e6
    if n >= 1e11:  # 毫秒量级（约 1973 年以后的 ms 时间戳）
        return "ms", 1e3
    return "s", 1.0


def _act_now(a):
    tz = _parse_tz(a.get("tz"))
    fmt = a.get("format") or _DEFAULT_FMT
    now_local = datetime.now().astimezone()
    target = now_local.astimezone(_tzinfo_or_local(tz))
    ts = int(now_local.timestamp())
    return ok({
        "timestamp": ts,
        "timestamp_ms": int(now_local.timestamp() * 1000),
        "local": now_local.strftime(fmt),
        "utc": datetime.now(timezone.utc).strftime(fmt),
        "iso": target.isoformat(),
        "tz": str(tz) if tz is not None else str(now_local.tzinfo),
    })


def _act_to_date(a):
    raw = a.get("value")
    if raw is None or str(raw).strip() == "":
        return error("缺少 value（时间戳，秒/毫秒/微秒均可）", code="missing_argument")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return error(f"value 必须是数字时间戳，收到: {raw!r}", code="bad_request")
    try:
        unit, div = _detect_unit(value, a.get("unit"))
        tz = _parse_tz(a.get("tz"))
    except ValueError as exc:
        return error(str(exc), code="bad_request")
    fmt = a.get("format") or _DEFAULT_FMT
    try:
        dt_utc = datetime.fromtimestamp(value / div, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return error(f"时间戳超出可表示范围: {raw!r}", code="bad_request")
    dt = dt_utc.astimezone(_tzinfo_or_local(tz))
    return ok({
        "input": value,
        "unit": unit,
        "timestamp": int(value / div),
        "datetime": dt.strftime(fmt),
        "iso": dt.isoformat(),
        "utc": dt_utc.strftime(fmt),
        "tz": str(tz) if tz is not None else str(dt.tzinfo),
        "weekday": dt.strftime("%A"),
    })


def _act_to_ts(a):
    raw = a.get("value")
    if raw is None or str(raw).strip() == "":
        return error("缺少 value（日期字符串，如 '2026-10-18 15:30:00'）", code="missing_argument")
    text = str(raw).strip()
    try:
        tz = _parse_tz(a.get("tz"))
    except ValueError as exc:
        return error(str(exc), code="bad_request")

    fmt = a.get("format")
    dt = None
    tried = []
    formats = [fmt] if fmt else list(_INPUT_FMTS)
    for f in formats:
        try:
            dt = datetime.strptime(text, f)
            break
        except (ValueError, TypeError):
            tried.append(f)
    if dt is None:
        return error(
            f"无法解析日期 '{text}'，已尝试格式: {', '.join(tried)}",
            code="bad_request",
        )

    dt = dt.replace(tzinfo=_tzinfo_or_local(tz))
    epoch = dt.timestamp()
    unit = str(a.get("unit") or "s").strip().lower()
    if unit == "s":
        out = int(epoch)
    elif unit == "ms":
        out = int(epoch * 1000)
    elif unit == "us":
        out = int(epoch * 1e6)
    else:
        return error(f"unit 必须是 s / ms / us，收到: {a.get('unit')!r}", code="bad_request")
    return ok({
        "input": text,
        "timestamp": out,
        "unit": unit,
        "datetime": dt.strftime(_DEFAULT_FMT),
        "iso": dt.isoformat(),
        "utc": dt.astimezone(timezone.utc).strftime(_DEFAULT_FMT),
        "tz": str(tz) if tz is not None else str(dt.tzinfo),
    })


_ACTIONS = {
    "now": _act_now,
    "to_date": _act_to_date,
    "to_ts": _act_to_ts,
}


def run(arguments):
    a = arguments or {}
    action = (a.get("action") or "").strip()
    fn = _ACTIONS.get(action)
    if not fn:
        return error(
            f"未知 action '{action}'，可用: {', '.join(_ACTIONS)}",
            code="unknown_action",
        )
    return fn(a)


if __name__ == "__main__":
    import json
    import sys

    arg_text = sys.argv[1] if len(sys.argv) > 1 else '{"action": "now"}'
    print(run(json.loads(arg_text)))
