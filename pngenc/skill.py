# skill: pngenc
# description: PNG 工具：draw 画图（零依赖或 Pillow 抗锯齿/文字）/ encode 原始像素 / decode 读回
# status: enable
# usage:
#   arguments.action = draw | encode | decode | selftest
#     draw      用形状直接出图（推荐）
#     encode    编码原始像素（pixels base64 或 input 文件）
#     decode    解析 PNG（校验 CRC，可 output_raw 导出像素）
#     selftest  内置样图回环自检
#   draw: width/height 必填；background 默认白色；colortype 可选 0/2/6（默认自动）
#     shapes 按序覆盖：rect / circle / line / polygon / gradient / checker / fill
#     装了 Pillow 时另支持 text（x/y/size/color/font/anchor），并默认超采样抗锯齿
#     engine = auto（默认，有 Pillow 就用）| plain（零依赖）| pillow（强制）
#     注意：移动端（android / kelivo）没有 Pillow，auto 会回落 plain，text 不可用
#     supersample = 1-4，默认 3（仅 pillow）；例 {"type":"circle","cx":60,"cy":40,"r":20,"color":"#ff8800"}
#     颜色：[r,g,b] / [r,g,b,a] / 灰度 0-255 / #rgb / #rrggbb / #rrggbbaa
#   encode: width/height + pixels|input；colortype 0/2/3/4/6（3 需 palette）
#     可选 trns / phys / text / filter(0-4) / backend(zlib|fixed|storage) / output
#   decode: path；可选 output_raw
#   selftest: size 默认 64（8-256）
#   output 默认 <技能目录>/png/png_<hash>.png，相对路径以技能目录为基准
#   正文显示图片：回答里用英文方括号包住「image:路径」；marker=true 时结果直接附带
#   示例: {"action":"draw","width":200,"height":120,"shapes":[
#            {"type":"gradient","direction":"diagonal","from":"#1e90ff","to":"#ffcc00"},
#            {"type":"circle","cx":100,"cy":60,"r":38,"color":[255,255,255]}]}
import base64
import binascii
import hashlib
import importlib.util
import struct
from pathlib import Path

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

try:
    from . import _canvas, _codec
except ImportError:
    import _canvas
    import _codec

_SKILL_DIR = Path(__file__).resolve().parent
_ACTIONS = ("draw", "encode", "decode", "selftest")
_MAX_PIXELS = 64 * 1024 * 1024
_HAVE_PILLOW = None


def _resolve(raw, must_exist=False):
    """相对路径按技能目录解析，返回 (Path, 错误信封)。"""
    if not isinstance(raw, str) or not raw.strip():
        return None, error("路径必须是非空字符串", code="bad_request")
    try:
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            path = _SKILL_DIR / path
        path = path.resolve()
    except (OSError, ValueError) as exc:
        return None, error(f"路径不合法: {exc}", code="bad_request")
    if must_exist and not path.is_file():
        return None, error(f"文件不存在: {path}", code="not_found")
    return path, None


def _output_path(a, digest):
    """确定 PNG 输出路径并创建父目录，返回 (path, 错误信封)。"""
    raw = a.get("output")
    if raw is not None and not isinstance(raw, str):
        return None, error("output 必须是字符串", code="bad_request")
    if raw and raw.strip():
        path, err = _resolve(raw)
        if err:
            return path, err
        if path.name in ("", ".", "..") or path.is_dir():
            return None, error("output 需要是文件路径，不能是目录", code="bad_request")
        if path.suffix.lower() != ".png":
            path = path.with_suffix(".png")
    else:
        path = _SKILL_DIR / "png" / f"png_{digest}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, error(f"创建输出目录失败: {exc}", code="io_error")
    return path, None


def _int(a, key, default=None, lo=None, hi=None):
    raw = a.get(key, default)
    if raw is None:
        return None, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
            raw = int(raw.strip())
        else:
            return None, error(f"{key} 必须是整数", code="bad_request")
    if lo is not None and raw < lo:
        return None, error(f"{key} 不能小于 {lo}", code="bad_request")
    if hi is not None and raw > hi:
        return None, error(f"{key} 不能大于 {hi}", code="bad_request")
    return raw, None


def _byte_array(a, key, allow_empty=True):
    raw = a.get(key)
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        return None, error(f"{key} 必须是整数数组", code="bad_request")
    if not raw and not allow_empty:
        return None, error(f"{key} 不能为空", code="bad_request")
    out = []
    for v in raw:
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255:
            return None, error(f"{key} 的元素必须是 0-255 整数", code="bad_request")
        out.append(v)
    return out, None


def _pixels(a):
    """从 pixels(base64) 或 input(文件) 取像素，返回 (bytes, 错误信封)。"""
    raw = a.get("pixels")
    src = a.get("input")
    if raw is not None and src:
        return None, error("pixels 与 input 只能二选一", code="bad_request")

    if raw is not None:
        if not isinstance(raw, str):
            return None, error("pixels 必须是 base64 字符串", code="bad_request")
        text = "".join(raw.split())
        if text.startswith("data:"):
            text = text.partition(",")[2] or text
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            return None, error("pixels 不是合法的 base64", code="bad_request")
    elif src:
        path, err = _resolve(src, must_exist=True)
        if err:
            return None, err
        try:
            data = path.read_bytes()
        except OSError as exc:
            return None, error(f"读取 input 失败: {exc}", code="io_error")
    else:
        return None, error("缺少像素数据：请提供 pixels(base64) 或 input(文件)", code="missing_argument")

    if not data:
        return None, error("像素数据为空", code="bad_request")
    if len(data) > _MAX_PIXELS:
        return None, error(f"像素数据过大（{len(data)} 字节，上限 {_MAX_PIXELS}）", code="too_large")
    return data, None


def _encode(a):
    width, err = _int(a, "width", lo=1, hi=100000)
    if err:
        return err
    height, err = _int(a, "height", lo=1, hi=100000)
    if err:
        return err
    if width is None or height is None:
        return error("缺少 width / height", code="missing_argument")

    pixels, err = _pixels(a)
    if err:
        return err

    colortype, err = _int(a, "colortype", lo=0, hi=6)
    if err:
        return err
    ch = len(pixels) // (width * height) if width * height else 0
    resolved_ct = colortype if colortype is not None else {1: 0, 2: 4, 3: 2, 4: 6}.get(ch)
    palette, err = _byte_array(a, "palette")
    if err:
        return err
    if palette is not None:
        if len(palette) % 3:
            return error("palette 长度必须是 3 的倍数（RGB 三元组）", code="bad_request")
        if len(palette) > 768:
            return error("palette 最多 256 个颜色（768 字节）", code="bad_request")
        if resolved_ct != 3:
            return error("palette 仅用于 colortype=3", code="bad_request")
    trns, err = _byte_array(a, "trns")
    if err:
        return err

    opts, err = _visual(a)
    if err:
        return err
    return _save(a, width, height, pixels, resolved_ct, opts, palette, trns)


def _visual(a):
    phys = a.get("phys")
    if phys is not None:
        if (not isinstance(phys, list) or len(phys) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in phys)):
            return None, error("phys 必须是两个正整数 [x, y]", code="bad_request")

    text = a.get("text")
    if text is not None:
        if not isinstance(text, dict):
            return None, error("text 必须是 {键: 值} 对象", code="bad_request")
        for k, v in text.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return None, error("text 的键和值都必须是字符串", code="bad_request")
            try:
                k.encode("latin-1")
                v.encode("latin-1")
            except UnicodeEncodeError:
                return None, error(f"text 仅支持 latin-1：{k}", code="bad_request")

    filter_ = a.get("filter")
    if filter_ in (None, "auto", ""):
        filter_ = None
    else:
        filter_, err = _int(a, "filter", lo=0, hi=4)
        if err:
            return None, err

    backend = str(a.get("backend") or "zlib").strip().lower()
    if backend not in _codec.BACKENDS:
        return None, error(
            f"不支持的 backend（可用 {'/'.join(_codec.BACKENDS)}）", code="bad_request"
        )
    return {"phys": phys, "text": text, "filter": filter_, "backend": backend}, None


def _has_pillow():
    global _HAVE_PILLOW
    if _HAVE_PILLOW is None:
        try:
            _HAVE_PILLOW = importlib.util.find_spec("PIL") is not None
        except (ImportError, ValueError):
            _HAVE_PILLOW = False
    return _HAVE_PILLOW


def _pillow_module():
    try:
        from . import _canvas_pillow
    except ImportError:
        import _canvas_pillow
    return _canvas_pillow


def _save(a, width, height, pixels, colortype, opts, palette=None, trns=None, extra=None):
    digest = hashlib.sha1(
        b"\0".join([
            str(width).encode(), str(height).encode(),
            str(colortype).encode(), opts["backend"].encode(),
            str(opts["filter"]).encode(), pixels,
        ])
    ).hexdigest()[:12]
    path, err = _output_path(a, digest)
    if err:
        return err

    try:
        blob = _codec.encode_png(
            width, height, pixels, colortype=colortype,
            palette=palette, trns=trns, phys=opts["phys"], text=opts["text"],
            filter_=opts["filter"], backend=opts["backend"],
        )
    except (ValueError, OverflowError, struct.error) as exc:
        return error(str(exc), code="bad_request")

    try:
        path.write_bytes(blob)
        size = path.stat().st_size
    except OSError as exc:
        return error(f"写入 PNG 失败: {exc}", code="io_error")

    marker = a.get("marker", False)
    if not isinstance(marker, bool):
        return error("marker 必须是布尔值", code="bad_request")
    payload = {
        "path": path.as_posix(),
        "width": width,
        "height": height,
        "colortype": colortype,
        "backend": opts["backend"],
        "size_bytes": size,
        "sha1": hashlib.sha1(blob).hexdigest(),
    }
    if marker:
        payload["image"] = f"[image:{path.as_posix()}]"
    payload.update(extra or {})
    return ok(payload)


def _draw(a):
    width, err = _int(a, "width", lo=1, hi=100000)
    if err:
        return err
    height, err = _int(a, "height", lo=1, hi=100000)
    if err:
        return err
    if width is None or height is None:
        return error("缺少 width / height", code="missing_argument")

    colortype, err = _int(a, "colortype", lo=0, hi=6)
    if err:
        return err
    if colortype is not None and colortype not in (0, 2, 6):
        return error("draw 的 colortype 仅支持 0 / 2 / 6", code="bad_request")

    shapes = a.get("shapes")
    if shapes is not None and not isinstance(shapes, list):
        return error("shapes 必须是数组", code="bad_request")
    shapes = shapes or []

    engine = str(a.get("engine") or "auto").strip().lower()
    if engine not in ("auto", "plain", "pillow"):
        return error("engine 只能是 auto / plain / pillow", code="bad_request")
    use_pillow = engine == "pillow" or (engine == "auto" and _has_pillow())
    if engine == "pillow" and not _has_pillow():
        return error("未安装 Pillow，无法使用 engine=pillow（可 pip install pillow）",
                     code="missing_dependency")

    opts, err = _visual(a)
    if err:
        return err

    if use_pillow:
        ss, err = _int(a, "supersample", default=3, lo=1, hi=4)
        if err:
            return err
        try:
            img = _pillow_module().render(
                width, height, a.get("background"), shapes, supersample=ss)
            rgba = img.tobytes()
            ct = colortype if colortype is not None else _canvas.colortype_of(rgba)
            pixels = _canvas.convert(rgba, ct)
        except _canvas.CanvasError as exc:
            return error(str(exc), code=exc.code)
        except (OSError, ValueError) as exc:
            return error(f"Pillow 渲染失败: {exc}", code="draw_error")
        return _save(a, width, height, pixels, ct, opts,
                     extra={"engine": "pillow", "supersample": ss})

    if any(isinstance(s, dict) and str(s.get("type") or "").lower() == "text"
           for s in shapes):
        return error("text 形状需要 Pillow：请装 pillow 或改 engine=auto",
                     code="missing_dependency")
    try:
        rgba, auto_ct = _canvas.render(width, height, a.get("background"), shapes)
        ct = colortype if colortype is not None else auto_ct
        pixels = _canvas.convert(rgba, ct)
    except _canvas.CanvasError as exc:
        return error(str(exc), code=exc.code)
    return _save(a, width, height, pixels, ct, opts, extra={"engine": "plain"})


def _decode(a):
    path, err = _resolve(a.get("path"), must_exist=True)
    if err:
        return err

    try:
        blob = path.read_bytes()
    except OSError as exc:
        return error(f"读取文件失败: {exc}", code="io_error")

    try:
        info = _codec.decode_png(blob)
    except RuntimeError as exc:
        return error(str(exc), code="missing_dependency")
    except (ValueError, struct.error) as exc:
        return error(f"PNG 解析失败: {exc}", code="bad_request")

    result = {
        "width": info["w"],
        "height": info["h"],
        "colortype": info["colortype"],
        "channels": _codec.COLOR_CHANNELS[info["colortype"]],
        "palette_entries": len(info["palette"]) // 3 if info["palette"] else 0,
        "chunks": info["chunks"],
        "size_bytes": len(blob),
        "pixels_sha1": hashlib.sha1(info["pixels"]).hexdigest(),
    }

    raw_out = a.get("output_raw")
    if raw_out is not None and str(raw_out).strip():
        out, err = _resolve(raw_out)
        if err:
            return err
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(info["pixels"])
        except OSError as exc:
            return error(f"写入原始像素失败: {exc}", code="io_error")
        result["raw"] = out.as_posix()
    return ok(result)


def _selftest(a):
    import random

    size, err = _int(a, "size", default=64, lo=8, hi=256)
    if err:
        return err
    px = _codec.demo_image(size, size)

    backends = {}
    for backend in _codec.BACKENDS:
        blob = _codec.encode_png(size, size, px, backend=backend,
                                 phys=(2835, 2835), text={"Software": "pngenc"})
        try:
            got = _codec.decode_png(blob)["pixels"]
            passed = got == px
        except Exception as exc:
            passed = False
            blob = blob[:0]
            backends[backend] = {"ok": False, "error": str(exc)}
            continue
        backends[backend] = {
            "ok": passed,
            "size_bytes": len(blob),
            "pixels_sha1": hashlib.sha1(got).hexdigest(),
        }

    filters = {}
    for name, choice in (("auto", None), ("none", 0), ("sub", 1),
                         ("up", 2), ("average", 3), ("paeth", 4)):
        filters[name] = len(_codec.encode_png(size, size, px, filter_=choice))

    random.seed(1)
    deflate_ok = 0
    if _codec.HAVE_ZLIB:
        for _ in range(20):
            data = bytes(random.randrange(256) for _ in range(random.randrange(0, 3000)))
            try:
                if _codec._z.decompress(_codec.deflate_fixed(data), -15) == data:
                    deflate_ok += 1
            except Exception:
                pass

    return ok({
        "size": size,
        "backends": backends,
        "filters_bytes": filters,
        "deflate_fixed_ok": f"{deflate_ok}/20",
    })


def run(arguments):
    a = arguments or {}
    if not isinstance(a, dict):
        return error("arguments 必须是对象", code="bad_request")

    action = str(a.get("action") or "").strip().lower()
    if not action:
        return error(f"缺少 action（可用 {'/'.join(_ACTIONS)}）", code="missing_argument")
    if action not in _ACTIONS:
        return error(f"未知 action（可用 {'/'.join(_ACTIONS)}）", code="unknown_action")

    if action == "encode":
        return _encode(a)
    if action == "draw":
        return _draw(a)
    if action == "decode":
        return _decode(a)
    return _selftest(a)
