# -*- coding: utf-8 -*-
"""Pillow 绘图后端：同样的形状描述，超采样抗锯齿 + 字体。

仅在检测到 Pillow 时由 skill 调用；未安装时仍走 _canvas 的零依赖实现。
坐标为最终像素坐标，内部统一乘以 supersample 渲染后再缩小。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from . import _canvas
except ImportError:
    import _canvas

CanvasError = _canvas.CanvasError
_SKILL_DIR = Path(__file__).resolve().parent

_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)


def _scale(value, ss):
    return int(round(value * ss))


def _area(width, height, shape, ss):
    box = _canvas._clip(width, height, shape)
    if not box:
        return None
    return tuple(v * ss for v in box)


def _lerp(c1, c2, f):
    inv = 255 - f
    return tuple((c1[i] * inv + c2[i] * f) // 255 for i in range(4))


def _rect(d, shape, ss, width, height):
    area = _area(width, height, shape, ss)
    if not area:
        return
    rgba = _canvas.color(shape.get("color", [0, 0, 0, 255]), "rect.color")
    x0, y0, x1, y1 = area
    if shape.get("fill", True):
        d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=rgba)
    else:
        t = max(1, _canvas._int(shape, "stroke_width", 1, lo=1) * ss)
        d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=rgba, width=t)


def _circle(d, shape, ss, width, height):
    cx = _canvas._int(shape, "cx") * ss
    cy = _canvas._int(shape, "cy") * ss
    r = _canvas._int(shape, "r", lo=0) * ss
    rgba = _canvas.color(shape.get("color", [0, 0, 0, 255]), "circle.color")
    box = [cx - r, cy - r, cx + r, cy + r]
    if shape.get("fill", True):
        d.ellipse(box, fill=rgba)
    else:
        t = max(1, _canvas._int(shape, "stroke_width", 1, lo=1) * ss)
        d.ellipse(box, outline=rgba, width=t)


def _line(d, shape, ss, width, height):
    pts = [(shape.get("x1"), shape.get("y1")), (shape.get("x2"), shape.get("y2"))]
    out = []
    for i, (x, y) in enumerate(pts):
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            raise CanvasError(f"{'x1/y1' if i == 0 else 'x2/y2'} 必须是整数")
        out.append((x * ss, y * ss))
    rgba = _canvas.color(shape.get("color", [0, 0, 0, 255]), "line.color")
    t = max(1, _canvas._int(shape, "width", 1, lo=1) * ss)
    d.line(out, fill=rgba, width=t, joint="curve")


def _polygon(d, shape, ss, width, height):
    pts = shape.get("points")
    if not isinstance(pts, list) or len(pts) < 2:
        raise CanvasError("polygon.points 至少需要 2 个点 [[x,y], ...]")
    points = []
    for i, pt in enumerate(pts):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise CanvasError(f"polygon.points[{i}] 必须是 [x, y]")
        x, y = pt
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            raise CanvasError(f"polygon.points[{i}] 坐标必须是整数")
        points.append((x * ss, y * ss))
    rgba = _canvas.color(shape.get("color", [0, 0, 0, 255]), "polygon.color")
    if shape.get("fill", True):
        if len(points) < 3:
            raise CanvasError("polygon 填充至少需要 3 个点")
        d.polygon(points, fill=rgba)
    else:
        t = max(1, _canvas._int(shape, "stroke_width", 1, lo=1) * ss)
        d.line(points + [points[0]], fill=rgba, width=t, joint="curve")


def _gradient(d, shape, ss, width, height):
    area = _area(width, height, shape, ss)
    if not area:
        return
    x0, y0, x1, y1 = area
    start = _canvas.color(shape.get("from"), "gradient.from")
    end = _canvas.color(shape.get("to"), "gradient.to")
    direction = str(shape.get("direction") or "horizontal").lower()
    if direction not in ("horizontal", "vertical", "diagonal"):
        raise CanvasError("gradient.direction 仅支持 horizontal / vertical / diagonal")
    w, h = x1 - x0, y1 - y0
    if direction == "horizontal":
        span = max(1, w - 1)
        for x in range(w):
            d.line([(x0 + x, y0), (x0 + x, y1 - 1)], fill=_lerp(start, end, x * 255 // span))
    elif direction == "vertical":
        span = max(1, h - 1)
        for y in range(h):
            d.line([(x0, y0 + y), (x1 - 1, y0 + y)], fill=_lerp(start, end, y * 255 // span))
    else:
        span = max(1, w + h - 2)
        for t in range(w + h - 1):
            u0, u1 = max(0, t - (h - 1)), min(t, w - 1)
            d.line([(x0 + u0, y0 + t - u0), (x0 + u1, y0 + t - u1)],
                   fill=_lerp(start, end, t * 255 // span))


def _checker(d, shape, ss, width, height):
    area = _area(width, height, shape, ss)
    if not area:
        return
    x0, y0, x1, y1 = area
    size = max(1, _canvas._int(shape, "size", 8, lo=1) * ss)
    c1 = _canvas.color(shape.get("color1", "#ffffff"), "checker.color1")
    c2 = _canvas.color(shape.get("color2", "#dddddd"), "checker.color2")
    for y in range((y0 // size) * size, y1, size):
        for x in range((x0 // size) * size, x1, size):
            cell = c1 if ((x // size) + (y // size)) & 1 else c2
            left, top = max(x, x0), max(y, y0)
            right, bottom = min(x + size, x1), min(y + size, y1)
            if right > left and bottom > top:
                d.rectangle([left, top, right - 1, bottom - 1], fill=cell)


def _fill(d, shape, ss, width, height):
    area = _area(width, height, shape, ss)
    if area:
        d.rectangle([area[0], area[1], area[2] - 1, area[3] - 1],
                    fill=_canvas.color(shape.get("color", [0, 0, 0, 255]), "fill.color"))


def _font(raw, px):
    if raw:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = _SKILL_DIR / path
        if not path.is_file():
            raise CanvasError(f"字体文件不存在: {raw}")
        try:
            return ImageFont.truetype(str(path), px)
        except OSError as exc:
            raise CanvasError(f"加载字体失败: {exc}")
    for candidate in _FONTS:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, px)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


def _text(d, shape, ss, width, height):
    value = shape.get("text")
    if not isinstance(value, str) or not value:
        raise CanvasError("text.text 必须是非空字符串")
    size = _canvas._int(shape, "size", 24, lo=1)
    font = _font(shape.get("font"), size * ss)
    rgba = _canvas.color(shape.get("color", [0, 0, 0, 255]), "text.color")
    x = _canvas._int(shape, "x", 0) * ss
    y = _canvas._int(shape, "y", 0) * ss
    anchor = str(shape.get("anchor") or "lt")
    try:
        d.text((x, y), value, font=font, fill=rgba, anchor=anchor)
    except ValueError as exc:
        raise CanvasError(f"text.anchor 不合法: {exc}")


_SHAPES = {
    "rect": _rect,
    "circle": _circle,
    "line": _line,
    "polygon": _polygon,
    "gradient": _gradient,
    "checker": _checker,
    "fill": _fill,
    "text": _text,
}
TYPES = tuple(_SHAPES)


def render(width, height, background=None, shapes=None, supersample=3):
    if width <= 0 or height <= 0:
        raise CanvasError("width / height 必须大于 0")
    ss = int(supersample)
    if not 1 <= ss <= 4:
        raise CanvasError("supersample 只能是 1-4")
    big_w, big_h = width * ss, height * ss
    if big_w * big_h * 4 > 128 * 1024 * 1024:
        raise CanvasError("画布过大（超采样后上限 128MB RGBA）", code="too_large")

    bg = _canvas.color(background if background is not None else [255, 255, 255, 255],
                       "background")
    base = Image.new("RGBA", (big_w, big_h), bg)

    for index, shape in enumerate(shapes or []):
        if not isinstance(shape, dict):
            raise CanvasError(f"shapes[{index}] 必须是对象")
        kind = str(shape.get("type") or "").strip().lower()
        handler = _SHAPES.get(kind)
        if handler is None:
            raise CanvasError(
                f"shapes[{index}] 未知 type（可用 {'/'.join(TYPES)}）"
            )
        layer = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
        try:
            handler(ImageDraw.Draw(layer), shape, ss, width, height)
        except CanvasError as exc:
            raise CanvasError(f"shapes[{index}]（{kind}）: {exc}", exc.code)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CanvasError(f"shapes[{index}]（{kind}）参数不合法: {exc}")
        base = Image.alpha_composite(base, layer)

    if ss > 1:
        base = base.resize((width, height), Image.LANCZOS)
    return base
