# skill: exif
# description: 图片 EXIF 工具（piexif）：读取信息 / 移除 GPS / 移除全部 EXIF / 写入信息 / 添加用户备注
# status: enable
# usage:
#   arguments.action = read | remove_gps | remove_all | write | comment
#   arguments.path / arguments.paths = 单文件 / 多文件路径
#   read       : 读 EXIF（arguments.ifd 可选 0th/Exif/GPS/1st/Interop）
#   remove_gps : 移除 GPS（多文件）
#   remove_all : 移除全部 EXIF（多文件）
#   write      : 写入 arguments.info = {标签名: 值}
#   comment    : 写备注 arguments.comment = "文本"（Exif.UserComment）
#   保存       : 默认原地写回；arguments.output（单文件=路径，多文件=目录）
#                或 arguments.suffix（就近另存「原名<后缀>.jpg」）则另存新文件
#   标签见 result.tags；常用: Make/Model/Artist/Copyright/DateTime/ImageDescription/
#   Orientation/DateTimeOriginal/UserComment/ExposureTime/FNumber/ISOSpeedRatings/
#   FocalLength/LensModel/GPSLatitude/GPSLongitude/GPSAltitude 等
#   示例: {"action":"read","path":"./a.jpg"}
#         {"action":"remove_gps","paths":["a.jpg","b.jpg"]}
#         {"action":"remove_all","path":"a.jpg","output":"./clean.jpg"}
#         {"action":"write","path":"a.jpg","info":{"Artist":"张三"},"suffix":"_tag"}
#         {"action":"comment","paths":["a.jpg","b.jpg"],"comment":"翻拍于武汉"}
"""图片 EXIF 工具 skill（基于 piexif）。

统一入口 run(arguments)，按 action 分发：

  read        读取 EXIF，按 IFD 结构化返回；GPS 额外换算成十进制度
  remove_gps  清空 GPS 区块（保留其余 EXIF）
  remove_all  移除全部 EXIF（等同 piexif.remove）
  write       写入/覆盖若干标准标签（名 -> 值，值按标签类型自动编码）
  comment     写用户备注到 Exif.UserComment（非 ASCII 自动用 Unicode 编码）

写回策略：默认原地插入到原文件；给了 output（另存路径 / 目录）或 suffix 时另存为新文件。
remove_* / write / comment 均支持多文件（path 或 paths）。

限制：piexif 的 insert/remove 只适用于 JPEG / WebP；其他格式会返回 piexif_error。
"""
import json
import os
import sys

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

try:
    import piexif
    import piexif.helper
except ImportError:  # 运行时提示安装，避免注册期整体导入失败
    piexif = None

IFD_KEYS = ("0th", "Exif", "GPS", "1st", "Interop")
ACTIONS = ("read", "remove_gps", "remove_all", "write", "comment")

# 内部指针标签（ExifOffset / GPSInfo 等），读取展示时隐藏，避免噪音
SKIP_TAGS = {0x8769, 0x8825}

# (显示名, IFD, piexif 常量名, 值类型)
# 值类型: ascii 文本 / short·long·byte 整数 / rational 单个分数 / gps 三分量分数 / usercomment 备注
TAG_TABLE = [
    ("ImageDescription", "0th", "ImageDescription", "ascii"),
    ("Make", "0th", "Make", "ascii"),
    ("Model", "0th", "Model", "ascii"),
    ("Software", "0th", "Software", "ascii"),
    ("DateTime", "0th", "DateTime", "ascii"),
    ("Artist", "0th", "Artist", "ascii"),
    ("Copyright", "0th", "Copyright", "ascii"),
    ("HostComputer", "0th", "HostComputer", "ascii"),
    ("Orientation", "0th", "Orientation", "short"),
    ("XResolution", "0th", "XResolution", "rational"),
    ("YResolution", "0th", "YResolution", "rational"),
    ("ResolutionUnit", "0th", "ResolutionUnit", "short"),

    ("DateTimeOriginal", "Exif", "DateTimeOriginal", "ascii"),
    ("DateTimeDigitized", "Exif", "DateTimeDigitized", "ascii"),
    ("SubSecTimeOriginal", "Exif", "SubSecTimeOriginal", "ascii"),
    ("OffsetTime", "Exif", "OffsetTime", "ascii"),
    ("UserComment", "Exif", "UserComment", "usercomment"),
    ("ExposureTime", "Exif", "ExposureTime", "rational"),
    ("FNumber", "Exif", "FNumber", "rational"),
    ("ApertureValue", "Exif", "ApertureValue", "rational"),
    ("FocalLength", "Exif", "FocalLength", "rational"),
    ("ExposureProgram", "Exif", "ExposureProgram", "short"),
    ("ISOSpeedRatings", "Exif", "ISOSpeedRatings", "short"),
    ("MeteringMode", "Exif", "MeteringMode", "short"),
    ("Flash", "Exif", "Flash", "short"),
    ("FocalLengthIn35mmFilm", "Exif", "FocalLengthIn35mmFilm", "short"),
    ("WhiteBalance", "Exif", "WhiteBalance", "short"),
    ("ColorSpace", "Exif", "ColorSpace", "short"),
    ("PixelXDimension", "Exif", "PixelXDimension", "long"),
    ("PixelYDimension", "Exif", "PixelYDimension", "long"),
    ("LensMake", "Exif", "LensMake", "ascii"),
    ("LensModel", "Exif", "LensModel", "ascii"),
    ("LensSerialNumber", "Exif", "LensSerialNumber", "ascii"),
    ("BodySerialNumber", "Exif", "BodySerialNumber", "ascii"),

    ("GPSLatitudeRef", "GPS", "GPSLatitudeRef", "ascii"),
    ("GPSLatitude", "GPS", "GPSLatitude", "gps"),
    ("GPSLongitudeRef", "GPS", "GPSLongitudeRef", "ascii"),
    ("GPSLongitude", "GPS", "GPSLongitude", "gps"),
    ("GPSAltitudeRef", "GPS", "GPSAltitudeRef", "byte"),
    ("GPSAltitude", "GPS", "GPSAltitude", "rational"),
    ("GPSTimeStamp", "GPS", "GPSTimeStamp", "gps"),
    ("GPSDateStamp", "GPS", "GPSDateStamp", "ascii"),

    ("InteropIndex", "Interop", "InteroperabilityIndex", "ascii"),
]

_IFD_ALIASES = {"0th": "0th", "image": "0th", "exif": "exif", "gps": "gps",
                "1st": "1st", "interop": "interop"}


def _build_maps():
    """构建「小写标签名 -> (ifd, tag_id, kind, 显示名)」与「ifd -> {tag_id: 显示名}」两张表。"""
    if piexif is None:
        return {}, {}
    objs = {"0th": piexif.ImageIFD, "1st": piexif.ImageIFD, "Exif": piexif.ExifIFD,
            "GPS": piexif.GPSIFD, "Interop": piexif.InteropIFD}
    name2spec, id2name = {}, {}
    for name, ifd, attr, kind in TAG_TABLE:
        tag_id = getattr(objs[ifd], attr)
        name2spec[name.lower()] = (ifd, tag_id, kind, name)
        id2name.setdefault(ifd, {})[tag_id] = name
    id2name["1st"] = dict(id2name.get("0th", {}))  # 缩略图 IFD 复用 0th 标签名
    return name2spec, id2name


NAME2SPEC, ID2NAME = _build_maps()


class ExifError(Exception):
    """可控错误：message 面向用户，code 为稳定短标识。"""

    def __init__(self, message, code="exif_failed"):
        super().__init__(message)
        self.code = code


# ============================ 取值 / 编码 ============================

def _collect_paths(a):
    """从 arguments 取路径列表：paths（数组/字符串）优先，其次 path。"""
    raw = a.get("paths")
    if isinstance(raw, (list, tuple)):
        paths = [str(p) for p in raw if str(p).strip()]
    elif isinstance(raw, str) and raw.strip():
        paths = [raw.strip()]
    else:
        paths = []
    if not paths:
        single = a.get("path")
        if single is not None and str(single).strip():
            paths = [str(single).strip()]
    return paths


def _resolve(name):
    """把标签名解析成 (ifd, tag_id, kind, 显示名)；支持 'Exif.LensModel' 形式的 IFD 前缀。"""
    raw = str(name).strip()
    prefix = None
    if "." in raw:
        prefix, _, raw = raw.partition(".")
    spec = NAME2SPEC.get(raw.lower())
    if not spec:
        raise ExifError(f"不支持的标签 '{name}'", code="bad_request")
    ifd, tag_id, kind, canon = spec
    if prefix:
        want = _IFD_ALIASES.get(prefix.strip().lower())
        if want is None:
            raise ExifError(f"未知 IFD 前缀 '{prefix}'（可用 0th/Exif/GPS/1st/Interop）", code="bad_request")
        if want != ifd.lower():
            raise ExifError(f"'{canon}' 属于 {ifd}，与前缀 {prefix} 不符", code="bad_request")
    return ifd, tag_id, kind, canon


def _as_bytes(value):
    """ASCII 标签值：优先 ASCII，含非 ASCII 时退化为 UTF-8。"""
    if isinstance(value, bytes):
        return value
    text = str(value)
    try:
        return text.encode("ascii")
    except UnicodeEncodeError:
        return text.encode("utf-8")


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ExifError(f"需要整数，收到 {value!r}", code="bad_request")


def _frac(value):
    """单个 RATIONAL：接受 (n,d) / 'n/d' / 整数 / 小数。"""
    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            return (int(value[0]), int(value[1]))
        raise ExifError(f"RATIONAL 需要 [分子, 分母]，收到 {value!r}", code="bad_request")
    if isinstance(value, str) and "/" in value:
        num, den = value.split("/", 1)
        return (int(float(num)), int(float(den)))
    if isinstance(value, bool):
        return (int(value), 1)
    if isinstance(value, int):
        return (value, 1)
    from fractions import Fraction
    f = Fraction(float(value)).limit_denominator(1000000)
    return (f.numerator, f.denominator)


def _dms(value):
    """GPS 三分量：接受 3 个分数，或一个十进制值（自动拆成 度/分/秒）。"""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(_frac(v) for v in value)
    degrees = abs(float(value))
    deg = int(degrees)
    minutes = int((degrees - deg) * 60)
    seconds = (degrees - deg - minutes / 60.0) * 3600.0
    return ((deg, 1), (minutes, 1), (int(round(seconds * 100)), 100))


def _usercomment(value):
    text = str(value)
    encoding = "ascii" if text.isascii() else "unicode"
    return piexif.helper.UserComment.dump(text, encoding)


def _coerce(kind, value):
    if kind == "ascii":
        return _as_bytes(value)
    if kind in ("short", "long", "byte"):
        return _as_int(value)
    if kind == "rational":
        return _frac(value)
    if kind == "gps":
        return _dms(value)
    if kind == "usercomment":
        return _usercomment(value)
    return value


# ============================ 读取 / 展示 ============================

def _decode(data):
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")
    return data


def _rat_str(pair):
    """(分子, 分母) -> 可读字符串。"""
    if not (isinstance(pair, tuple) and len(pair) == 2):
        return pair
    num, den = pair
    if den == 0:
        return f"{num}/0"
    fval = num / den
    if float(num).is_integer() and float(den) == 1.0:
        return str(int(num))
    return f"{round(fval, 6):g}"


def _norm_value(kind, value):
    if kind == "usercomment":
        try:
            return piexif.helper.UserComment.load(value)
        except (ValueError, TypeError):
            return _decode(value)
    if isinstance(value, bytes):
        return _decode(value)
    if isinstance(value, tuple) and len(value) == 2 and all(isinstance(x, int) for x in value):
        return _rat_str(value)
    if isinstance(value, (list, tuple)):
        return [_rat_str(x) if (isinstance(x, tuple) and len(x) == 2) else _decode(x)
                for x in value]
    return value


def _read_ifds(exif_dict, only=None):
    want = [only] if only else list(IFD_KEYS)
    out = {}
    for ifd in want:
        if ifd not in exif_dict:
            continue
        names = ID2NAME.get(ifd, {})
        block = {}
        for tag_id, raw in exif_dict.get(ifd, {}).items():
            if tag_id in SKIP_TAGS:
                continue
            name = names.get(tag_id, "Tag_0x%04x" % tag_id)
            spec = NAME2SPEC.get(name.lower())
            kind = spec[2] if spec else None
            block[name] = _norm_value(kind, raw)
        if block:
            out[ifd] = block
    return out


def _has_exif(exif_dict):
    """是否含 EXIF：按原始数据判断（含缩略图），不受展示层过滤指针标签的影响。"""
    if exif_dict.get("thumbnail"):
        return True
    return any(exif_dict.get(ifd) for ifd in IFD_KEYS)


def _gps_block(exif_dict):
    gps = exif_dict.get("GPS") or {}
    lat = gps.get(piexif.GPSIFD.GPSLatitude)
    lon = gps.get(piexif.GPSIFD.GPSLongitude)
    lat_ref = gps.get(piexif.GPSIFD.GPSLatitudeRef)
    lon_ref = gps.get(piexif.GPSIFD.GPSLongitudeRef)
    alt = gps.get(piexif.GPSIFD.GPSAltitude)
    alt_ref = gps.get(piexif.GPSIFD.GPSAltitudeRef)
    block = {}
    if lat and lat_ref:
        block["latitude"] = _decimal(lat, _decode(lat_ref))
    if lon and lon_ref:
        block["longitude"] = _decimal(lon, _decode(lon_ref))
    if alt:
        value = _rat_of(alt)
        block["altitude"] = round(-value if alt_ref == 1 else value, 3)
    return block


def _rat_of(pair):
    num, den = pair
    return num / den if den else 0.0


def _decimal(dms, ref):
    deg = _rat_of(dms[0]) + _rat_of(dms[1]) / 60.0 + _rat_of(dms[2]) / 3600.0
    return round(-deg if str(ref).upper() in ("S", "W") else deg, 7)


# ============================ 保存目标 ============================

def _ensure_parent(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def _target(src, output, suffix, many):
    """算出保存路径；返回 (目标路径, 是否原地)。"""
    base = os.path.basename(src)
    if suffix:
        root, ext = os.path.splitext(base)
        base = root + str(suffix) + ext
    if not output:
        if not suffix:
            return src, True
        return os.path.join(os.path.dirname(os.path.abspath(src)), base), False
    if many or os.path.isdir(output):
        return os.path.join(output, base), False
    return output, False


def _apply(src, dst, exif_dict, inplace):
    """把 exif_dict dump 出来并写回：原地或另存为 dst。"""
    data = piexif.dump(exif_dict)
    if inplace:
        piexif.insert(data, src)
    else:
        _ensure_parent(dst)
        piexif.insert(data, src, dst)


# ============================ 各 action ============================

def _do_read(paths, a):
    ifd_filter = a.get("ifd")
    if ifd_filter is not None:
        ifd_filter = _IFD_ALIASES.get(str(ifd_filter).strip().lower())
        if ifd_filter is None:
            return error(f"未知 ifd '{a.get('ifd')}'（可用 0th/Exif/GPS/1st/Interop）", code="bad_request")

    items, errors = [], []
    for src in paths:
        if not os.path.isfile(src):
            errors.append({"path": src, "error": "文件不存在"})
            continue
        try:
            exif_dict = piexif.load(src)
        except Exception as exc:  # piexif 对非 JPEG/无 EXIF 抛各种异常
            errors.append({"path": src, "error": f"{type(exc).__name__}: {exc}"})
            continue
        ifds = _read_ifds(exif_dict, ifd_filter)
        item = {"path": src, "has_exif": _has_exif(exif_dict), "ifds": ifds}
        gps = _gps_block(exif_dict)
        if gps:
            item["gps"] = gps
        items.append(item)

    result = {"action": "read", "count": len(items), "items": items,
              "tags": sorted(NAME2SPEC[k][3] for k in NAME2SPEC)}
    if errors:
        result["errors"] = errors
    return ok(result)


def _do_remove(paths, a, remove_all):
    output = a.get("output")
    output = str(output).strip() if output else ""
    suffix = a.get("suffix")
    suffix = str(suffix).strip() if suffix else ""
    many = len(paths) > 1

    items, errors = [], []
    for src in paths:
        if not os.path.isfile(src):
            errors.append({"path": src, "error": "文件不存在"})
            continue
        dst, inplace = _target(src, output, suffix, many)
        try:
            if remove_all:
                if inplace:
                    piexif.remove(src)
                else:
                    _ensure_parent(dst)
                    piexif.remove(src, dst)
            else:
                exif_dict = piexif.load(src)
                exif_dict["GPS"] = {}
                _apply(src, dst, exif_dict, inplace)
        except Exception as exc:
            errors.append({"path": src, "error": f"{type(exc).__name__}: {exc}"})
            continue
        items.append({"path": src, "saved_to": None if inplace else dst,
                      "mode": "inplace" if inplace else "new_file"})

    action = "remove_all" if remove_all else "remove_gps"
    if not items and errors:
        return error(errors[0]["error"], code="exif_failed")
    result = {"action": action, "count": len(items), "items": items}
    if errors:
        result["errors"] = errors
    return ok(result)


def _parse_info(a):
    info = a.get("info")
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except json.JSONDecodeError as exc:
            raise ExifError(f"arguments.info 不是合法 JSON: {exc}", code="bad_request")
    if not isinstance(info, dict) or not info:
        raise ExifError("write 需要 arguments.info 为非空对象，如 {\"Artist\": \"张三\"}", code="bad_request")
    return info


def _do_write(paths, a, comment_only):
    if comment_only:
        text = a.get("comment")
        if text is None or not str(text).strip():
            return error("comment 需要 arguments.comment 文本", code="bad_request")
        pairs = [("UserComment", str(text))]
    else:
        try:
            info = _parse_info(a)
        except ExifError as exc:
            return error(str(exc), code=exc.code)
        pairs = list(info.items())

    resolved = []
    for name, value in pairs:
        try:
            resolved.append((*_resolve(name), value))
        except ExifError as exc:
            return error(str(exc), code=exc.code)

    output = a.get("output")
    output = str(output).strip() if output else ""
    suffix = a.get("suffix")
    suffix = str(suffix).strip() if suffix else ""
    many = len(paths) > 1

    items, errors = [], []
    for src in paths:
        if not os.path.isfile(src):
            errors.append({"path": src, "error": "文件不存在"})
            continue
        dst, inplace = _target(src, output, suffix, many)
        try:
            exif_dict = piexif.load(src)
            for ifd, tag_id, kind, canon, value in resolved:
                exif_dict.setdefault(ifd, {})[tag_id] = _coerce(kind, value)
            _apply(src, dst, exif_dict, inplace)
        except Exception as exc:
            errors.append({"path": src, "error": f"{type(exc).__name__}: {exc}"})
            continue
        items.append({"path": src, "saved_to": None if inplace else dst,
                      "mode": "inplace" if inplace else "new_file",
                      "tags": [canon for _, _, _, canon, _ in resolved]})

    if not items and errors:
        return error(errors[0]["error"], code="exif_failed")
    action = "comment" if comment_only else "write"
    result = {"action": action, "count": len(items), "items": items}
    if errors:
        result["errors"] = errors
    return ok(result)


def run(arguments):
    """skill 入口：arguments -> ok()/error() 信封。"""
    a = arguments or {}

    if piexif is None:
        return error("未安装 piexif，请先执行 pip install piexif", code="not_installed")

    action = str(a.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return error(f"arguments.action 只能是 {' / '.join(ACTIONS)}，收到 '{action}'", code="bad_request")

    paths = _collect_paths(a)
    if not paths:
        return error("缺少 arguments.path 或 arguments.paths", code="bad_request")

    if action == "read":
        return _do_read(paths, a)
    if action == "remove_gps":
        return _do_remove(paths, a, remove_all=False)
    if action == "remove_all":
        return _do_remove(paths, a, remove_all=True)
    if action == "comment":
        return _do_write(paths, a, comment_only=True)
    return _do_write(paths, a, comment_only=False)


if __name__ == "__main__":  # 本地调试：python skill.py '{"action":"read","path":"a.jpg"}'
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
