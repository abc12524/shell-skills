# -*- coding: utf-8 -*-
"""极简绘图：把形状描述渲染成 RGBA 像素（零依赖）。

支持的形状（按数组顺序覆盖绘制）：
  rect / circle / line / gradient / checker / fill
坐标为像素整数，允许超出画布（自动裁剪）；颜色支持 [r,g,b] / [r,g,b,a] /
0-255 灰度 / "#rgb" / "#rrggbb" / "#rrggbbaa"。
"""
SHAPES = ("rect", "circle", "line", "polygon", "gradient", "checker", "fill")


class CanvasError(ValueError):
    def __init__(self, message, code="bad_request"):
        super().__init__(message)
        self.code = code


def color(value, field="color"):
    if isinstance(value, bool):
        raise CanvasError(f"{field} 不是合法颜色")
    if isinstance(value, int):
        if not 0 <= value <= 255:
            raise CanvasError(f"{field} 灰度必须是 0-255")
        return (value, value, value, 255)
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        if len(text) == 6:
            text += "ff"
        if len(text) != 8:
            raise CanvasError(f"{field} 需要 #rgb / #rrggbb / #rrggbbaa")
        try:
            v = int(text, 16)
        except ValueError:
            raise CanvasError(f"{field} 不是合法十六进制颜色")
        return ((v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255)
    if isinstance(value, (list, tuple)):
        if len(value) == 3:
            value = list(value) + [255]
        if len(value) != 4:
            raise CanvasError(f"{field} 需要 [r,g,b] 或 [r,g,b,a]")
        out = []
        for v in value:
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255:
                raise CanvasError(f"{field} 的分量必须是 0-255 整数")
            out.append(v)
        return tuple(out)
    raise CanvasError(f"{field} 不是合法颜色")


def _int(shape, key, default=None, lo=None):
    raw = shape.get(key, default)
    if raw is None:
        if default is None:
            raise CanvasError(f"缺少 {key}")
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise CanvasError(f"{key} 必须是整数")
    if lo is not None and raw < lo:
        raise CanvasError(f"{key} 不能小于 {lo}")
    return raw


def _clip(width, height, shape):
    x = _int(shape, "x", 0)
    y = _int(shape, "y", 0)
    w = _int(shape, "w", width)
    h = _int(shape, "h", height)
    if w <= 0 or h <= 0:
        return None
    return max(0, x), max(0, y), min(width, x + w), min(height, y + h)


def _put(px, width, height, x, y, rgba):
    if 0 <= x < width and 0 <= y < height:
        i = (y * width + x) * 4
        a = rgba[3]
        if a == 255:
            px[i:i + 4] = bytes(rgba)
        elif a:
            inv = 255 - a
            da = px[i + 3]
            px[i] = (rgba[0] * a + px[i] * inv) // 255
            px[i + 1] = (rgba[1] * a + px[i + 1] * inv) // 255
            px[i + 2] = (rgba[2] * a + px[i + 2] * inv) // 255
            px[i + 3] = a + da * inv // 255


def _fill_rect(px, width, height, area, rgba):
    x0, y0, x1, y1 = area
    if rgba[3] != 255:
        for y in range(y0, y1):
            for x in range(x0, x1):
                _put(px, width, height, x, y, rgba)
        return
    line = bytes(rgba) * (x1 - x0)
    for y in range(y0, y1):
        i = (y * width + x0) * 4
        px[i:i + len(line)] = line


def _rect(px, width, height, shape):
    area = _clip(width, height, shape)
    if not area:
        return
    rgba = color(shape.get("color", [0, 0, 0, 255]), "rect.color")
    if shape.get("fill", True):
        _fill_rect(px, width, height, area, rgba)
        return
    t = _int(shape, "stroke_width", 1, lo=1)
    x0, y0, x1, y1 = area
    _fill_rect(px, width, height, (x0, y0, x1, min(y0 + t, y1)), rgba)
    _fill_rect(px, width, height, (x0, max(y1 - t, y0), x1, y1), rgba)
    _fill_rect(px, width, height, (x0, y0, min(x0 + t, x1), y1), rgba)
    _fill_rect(px, width, height, (max(x1 - t, x0), y0, x1, y1), rgba)


def _circle(px, width, height, shape):
    cx = _int(shape, "cx")
    cy = _int(shape, "cy")
    r = _int(shape, "r", lo=0)
    rgba = color(shape.get("color", [0, 0, 0, 255]), "circle.color")
    filled = shape.get("fill", True)
    t = _int(shape, "stroke_width", 1, lo=1)
    inner = max(0, r - t) if not filled else -1
    y0, y1 = max(0, cy - r), min(height - 1, cy + r)
    x0, x1 = max(0, cx - r), min(width - 1, cx + r)
    for y in range(y0, y1 + 1):
        dy = y - cy
        for x in range(x0, x1 + 1):
            dx = x - cx
            d2 = dx * dx + dy * dy
            if d2 <= r * r and (filled or d2 > inner * inner):
                _put(px, width, height, x, y, rgba)


def _line(px, width, height, shape):
    x1 = _int(shape, "x1")
    y1 = _int(shape, "y1")
    x2 = _int(shape, "x2")
    y2 = _int(shape, "y2")
    rgba = color(shape.get("color", [0, 0, 0, 255]), "line.color")
    t = _int(shape, "width", 1, lo=1)
    off = range(-(t // 2), t - t // 2)
    dx, dy = abs(x2 - x1), -abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx + dy
    while True:
        for oy in off:
            for ox in off:
                _put(px, width, height, x1 + ox, y1 + oy, rgba)
        if x1 == x2 and y1 == y2:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x1 += sx
        if e2 <= dx:
            err += dx
            y1 += sy


def _polygon(px, width, height, shape):
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
        points.append((x, y))

    rgba = color(shape.get("color", [0, 0, 0, 255]), "polygon.color")
    if shape.get("fill", True):
        if len(points) < 3:
            raise CanvasError("polygon 填充至少需要 3 个点")
        ys = [p[1] for p in points]
        y0, y1 = max(0, min(ys)), min(height - 1, max(ys))
        n = len(points)
        for y in range(y0, y1 + 1):
            xs = []
            for i in range(n):
                x1, y1e = points[i]
                x2, y2 = points[(i + 1) % n]
                if y1e == y2:
                    continue
                if min(y1e, y2) <= y < max(y1e, y2):
                    xs.append(x1 + (y - y1e) * (x2 - x1) / (y2 - y1e))
            if not xs:
                continue
            xs.sort()
            for a, b in zip(xs[0::2], xs[1::2]):
                left = max(0, int(round(a)))
                right = min(width, int(round(b)))
                if right > left:
                    _fill_rect(px, width, height, (left, y, right, y + 1), rgba)
    else:
        t = _int(shape, "stroke_width", 1, lo=1)
        for i in range(len(points)):
            x1, y1e = points[i]
            x2, y2 = points[(i + 1) % len(points)]
            _line(px, width, height, {
                "x1": x1, "y1": y1e, "x2": x2, "y2": y2,
                "color": list(rgba), "width": t,
            })


def _gradient(px, width, height, shape):
    area = _clip(width, height, shape)
    if not area:
        return
    start = color(shape.get("from"), "gradient.from")
    end = color(shape.get("to"), "gradient.to")
    direction = str(shape.get("direction") or "horizontal").lower()
    if direction not in ("horizontal", "vertical", "diagonal"):
        raise CanvasError("gradient.direction 仅支持 horizontal / vertical / diagonal")
    x0, y0, x1, y1 = area
    span_x = max(1, x1 - x0 - 1)
    span_y = max(1, y1 - y0 - 1)
    for y in range(y0, y1):
        for x in range(x0, x1):
            if direction == "horizontal":
                num, den = x - x0, span_x
            elif direction == "vertical":
                num, den = y - y0, span_y
            else:
                num, den = (x - x0) + (y - y0), span_x + span_y
            f = min(255, num * 255 // den)
            _put(px, width, height, x, y, tuple(
                (start[i] * (255 - f) + end[i] * f) // 255 for i in range(4)
            ))


def _checker(px, width, height, shape):
    area = _clip(width, height, shape)
    if not area:
        return
    size = _int(shape, "size", 8, lo=1)
    c1 = color(shape.get("color1", "#ffffff"), "checker.color1")
    c2 = color(shape.get("color2", "#dddddd"), "checker.color2")
    x0, y0, x1, y1 = area
    for y in range(y0, y1):
        for x in range(x0, x1):
            _put(px, width, height, x, y, c1 if ((x // size) + (y // size)) & 1 else c2)


def _fill(px, width, height, shape):
    area = _clip(width, height, shape)
    if area:
        _fill_rect(px, width, height, area, color(shape.get("color", [0, 0, 0, 255]), "fill.color"))


_DRAW = {
    "rect": _rect,
    "circle": _circle,
    "line": _line,
    "polygon": _polygon,
    "gradient": _gradient,
    "checker": _checker,
    "fill": _fill,
}


def render(width, height, background=None, shapes=None):
    """渲染成 (RGBA bytes, colortype)。colortype: 6 / 2 / 0。"""
    if width <= 0 or height <= 0:
        raise CanvasError("width / height 必须大于 0")
    if width * height * 4 > 64 * 1024 * 1024:
        raise CanvasError("画布过大（上限 64MB RGBA）", code="too_large")

    bg = color(background if background is not None else [255, 255, 255, 255], "background")
    px = bytearray(bytes(bg) * (width * height))

    for index, shape in enumerate(shapes or []):
        if not isinstance(shape, dict):
            raise CanvasError(f"shapes[{index}] 必须是对象")
        kind = str(shape.get("type") or "").strip().lower()
        handler = _DRAW.get(kind)
        if handler is None:
            raise CanvasError(
                f"shapes[{index}] 未知 type（可用 {'/'.join(SHAPES)}）"
            )
        try:
            handler(px, width, height, shape)
        except CanvasError as exc:
            raise CanvasError(f"shapes[{index}]（{kind}）: {exc}", exc.code)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CanvasError(f"shapes[{index}]（{kind}）参数不合法: {exc}")

    return bytes(px), _colortype(px)


def colortype_of(px):
    """按像素推断最小可用 colortype：0=灰度 / 2=RGB / 6=RGBA。"""
    return _colortype(px)


def _colortype(px):
    alpha = px[3::4]
    if any(a != 255 for a in alpha):
        return 6
    for i in range(0, len(px), 4):
        if px[i] != px[i + 1] or px[i] != px[i + 2]:
            return 2
    return 0


def convert(px, colortype):
    """把 RGBA 像素转成目标通道数；不兼容时抛 CanvasError。"""
    if colortype == 6:
        return px
    if colortype == 2:
        if any(a != 255 for a in px[3::4]):
            raise CanvasError("存在透明像素，colortype=2 无法保存，请用 colortype=6 或不指定")
        out = bytearray(len(px) // 4 * 3)
        j = 0
        for i in range(0, len(px), 4):
            out[j:j + 3] = px[i:i + 3]
            j += 3
        return bytes(out)
    if colortype == 0:
        if any(a != 255 for a in px[3::4]):
            raise CanvasError("存在透明像素，colortype=0 无法保存，请用 colortype=6 或不指定")
        for i in range(0, len(px), 4):
            if px[i] != px[i + 1] or px[i] != px[i + 2]:
                raise CanvasError("画布含彩色像素，colortype=0 无法保存，请用 colortype=2/6 或不指定")
        return bytes(px[0::4])
    raise CanvasError(f"draw 不支持 colortype={colortype}（可用 0/2/6）")
