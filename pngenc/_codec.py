# -*- coding: utf-8 -*-
"""PNG 编解码核心（零第三方依赖）。

包含手写 CRC-32 / Adler-32、chunk 封装、五种行滤波与自适应择优、
zlib / 固定哈夫曼 DEFLATE / 存储块三种后端，以及 decode_png() 回读自检。
"""
import struct

try:
    import zlib as _z
except ImportError:  # 极端裁剪的运行时：编码仍可走 fixed / storage
    _z = None

HAVE_ZLIB = _z is not None

SIG = b"\x89PNG\r\n\x1a\n"

# ======================= 1. 校验和 =======================
def _crc_table():
    t = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (0xEDB88320 ^ (c >> 1)) if (c & 1) else (c >> 1)
        t.append(c)
    return t

_CRC = _crc_table()

def crc32(data, crc=0):
    """标准 PNG CRC: 多项式 0xEDB88320 反射实现"""
    crc ^= 0xFFFFFFFF
    for b in data:
        crc = _CRC[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF

def adler32(data):
    a, b = 1, 0
    for i in range(0, len(data), 5552):
        for x in data[i:i + 5552]:
            a += x
            b += a
        a %= 65521
        b %= 65521
    return (b << 16) | a

# ======================= 2. chunk =======================
def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", crc32(tag + data)))

# ======================= 3. 行滤波 =======================
def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c

def _filter(ft, row, prev, n, bpp):
    if ft == 0:
        return bytes(row)
    o = bytearray(n)
    if ft == 1:      # Sub
        for i in range(n):
            o[i] = (row[i] - (row[i - bpp] if i >= bpp else 0)) & 0xFF
    elif ft == 2:    # Up
        for i in range(n):
            o[i] = (row[i] - prev[i]) & 0xFF
    elif ft == 3:    # Average
        for i in range(n):
            a = row[i - bpp] if i >= bpp else 0
            o[i] = (row[i] - ((a + prev[i]) >> 1)) & 0xFF
    else:            # Paeth
        for i in range(n):
            a = row[i - bpp] if i >= bpp else 0
            c = prev[i - bpp] if i >= bpp else 0
            o[i] = (row[i] - _paeth(a, prev[i], c)) & 0xFF
    return bytes(o)

def filter_scanlines(raw, width, height, bpp, choice=None):
    """逐行加 1 字节滤波类型前缀。choice=None 表示按绝对值熵自适应择优。"""
    n = width * bpp
    out = bytearray()
    prev = bytes(n)
    for y in range(height):
        row = raw[y * n:(y + 1) * n]
        if choice is None:
            best, bf, bs = None, 0, None
            for ft in range(5):
                f = _filter(ft, row, prev, n, bpp)
                s = sum(v if v < 128 else 256 - v for v in f)
                if bs is None or s < bs:
                    bs, best, bf = s, f, ft
        else:
            bf, best = choice, _filter(choice, row, prev, n, bpp)
        out.append(bf)
        out += best
        prev = row
    return bytes(out)

# ======================= 4a. 后端: zlib =======================
def zlib_compress(data, level=9):
    if _z is None:
        return zlib_stream(deflate_fixed(data), data)
    return _z.compress(data, level)

# ======================= 4b. 后端: 手写 DEFLATE =======================
class BitWriter:
    """LSB-first 位流；Huffman 码按高位先写入。"""
    __slots__ = ("out", "cur", "pos")
    def __init__(self):
        self.out, self.cur, self.pos = bytearray(), 0, 0
    def bit(self, b):
        if b:
            self.cur |= 1 << self.pos
        self.pos += 1
        if self.pos == 8:
            self.out.append(self.cur)
            self.cur, self.pos = 0, 0
    def put(self, code, n):           # Huffman 码: MSB first
        for i in range(n - 1, -1, -1):
            self.bit((code >> i) & 1)
    def putv(self, val, n):           # 附加位: LSB first
        for i in range(n):
            self.bit((val >> i) & 1)
    def flush(self):
        if self.pos:
            self.out.append(self.cur)
            self.cur, self.pos = 0, 0
        return bytes(self.out)

LEN_BASE = (3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,
            35,43,51,59,67,83,99,115,131,163,195,227,258)
LEN_EXTRA = (0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,
             3,3,3,3,4,4,4,4,5,5,5,5,0)
DIST_BASE = (1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,
             257,385,513,769,1025,1537,2049,3073,4097,6145,8193,12289,16385,24577)
DIST_EXTRA = (0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,
              7,7,8,8,9,9,10,10,11,11,12,12,13,13)

def _lit_code(sym):
    if sym <= 143:
        return 0x30 + sym, 8
    if sym <= 255:
        return 0x190 + sym - 144, 9
    if sym <= 279:
        return sym - 256, 7
    return 0xC0 + sym - 280, 8

def _len_code(n):
    for i in range(28, -1, -1):
        if n >= LEN_BASE[i]:
            return 257 + i, n - LEN_BASE[i], LEN_EXTRA[i]
    return 257, 0, 0

def _dist_code(d):
    for i in range(29, -1, -1):
        if d >= DIST_BASE[i]:
            return i, d - DIST_BASE[i], DIST_EXTRA[i]
    return 0, 0, 0

def lz77(data, max_chain=24, window=32768):
    """贪心 LZ77，返回 [(lit,)] 或 [(0, length, dist)] 序列。"""
    n = len(data)
    head, out, i = {}, [], 0
    while i < n:
        best, bestd = 0, 0
        if i + 3 <= n:
            lst = head.get(data[i:i+3])
            if lst:
                limit = min(258, n - i)
                for cnt, p in enumerate(reversed(lst)):
                    if cnt >= max_chain:
                        break
                    d = i - p
                    if d > window:
                        break
                    l = 0
                    while l < limit and data[p + l] == data[i + l]:
                        l += 1
                    if l > best:
                        best, bestd = l, d
                        if l == limit:
                            break
        if best >= 3:
            out.append((0, best, bestd))
            end = i + best
            while i < end:
                lst = head.setdefault(data[i:i+3], [])
                lst.append(i)
                if len(lst) > 2 * max_chain:
                    del lst[:len(lst) // 2]
                i += 1
        else:
            out.append((data[i],))
            lst = head.setdefault(data[i:i+3], [])
            lst.append(i)
            if len(lst) > 2 * max_chain:
                del lst[:len(lst) // 2]
            i += 1
    return out

def deflate_fixed(data):
    """BFINAL=1, BTYPE=01(固定哈夫曼) 的完整 deflate 流。"""
    bw = BitWriter()
    bw.bit(1)          # BFINAL
    bw.putv(1, 2)      # BTYPE=01，2bit 字段是 LSB-first 读的，必须用 putv
    for tok in lz77(data):
        if len(tok) == 1:
            c, n = _lit_code(tok[0])
            bw.put(c, n)
        else:
            _, length, dist = tok
            sym, extra, en = _len_code(length)
            c, n = _lit_code(sym)      # 注意: 要写的是码值 c，不是符号值 sym
            bw.put(c, n)
            bw.putv(extra, en)
            dc, dextra, dn = _dist_code(dist)
            bw.put(dc, 5)      # 固定距离码 5bit，Huffman 规则 MSB-first
            bw.putv(dextra, dn)
    c, n = _lit_code(256)          # 结束块
    bw.put(c, n)
    return bw.flush()

def zlib_stream(raw_deflate, original):
    """加 2 字节 zlib 头 + 4 字节 Adler-32 尾 -> 完整 zlib 流。"""
    return b"\x78\x9c" + raw_deflate + struct.pack(">I", adler32(original))

def stored_deflate(data, blocksize=65535):
    """最朴素的 deflate：BFINAL/BTYPE=00 + 原始字节（无压缩，但绝对合法）"""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n or n == 0:
        blk = data[i:i+blocksize]
        final = 1 if i + len(blk) >= n else 0
        out.append(final)                       # BFINAL + BTYPE=00，字节对齐
        out += struct.pack("<HH", len(blk), 0xFFFF ^ len(blk))
        out += blk
        i += len(blk)
        if n == 0:
            break
    return bytes(out)

# ======================= 5. 编码主函数 =======================
COLOR_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
BACKENDS = ("zlib", "fixed", "storage")

def encode_png(width, height, pixels, colortype=None, bitdepth=8,
               palette=None, trns=None, phys=None, text=None,
               filter_=None, backend="zlib"):
    """pixels: bytes/bytearray/可迭代，长度 = height * width * bpp。"""
    pixels = bytes(pixels)
    if colortype is None:
        cnt = {1: 0, 2: 4, 3: 2, 4: 6}
        ch = len(pixels) // (width * height) if width * height else 0
        colortype = cnt.get(ch)
        if colortype is None:
            raise ValueError("无法推断 colortype，请显式传入")
    if colortype not in COLOR_CHANNELS:
        raise ValueError(f"不支持的 colortype: {colortype}")
    if backend not in BACKENDS:
        raise ValueError(f"不支持的 backend: {backend}")
    if filter_ is not None and filter_ not in (0, 1, 2, 3, 4):
        raise ValueError(f"不支持的 filter: {filter_}")
    bpp = COLOR_CHANNELS[colortype]
    stride = width * bpp
    if len(pixels) != stride * height:
        raise ValueError(f"像素长度 {len(pixels)} != {stride * height}")
    if colortype == 3 and not palette:
        raise ValueError("调色板图必须提供 palette")

    ihdr = struct.pack(">IIBBBBB", width, height, bitdepth, colortype, 0, 0, 0)
    out = bytearray(SIG)
    out += chunk(b"IHDR", ihdr)
    if phys:
        out += chunk(b"pHYs", struct.pack(">IIB", phys[0], phys[1], 1))
    if colortype == 3:
        out += chunk(b"PLTE", bytes(palette))
    if trns:
        out += chunk(b"tRNS", bytes(trns))
    for k, v in (text or {}).items():
        out += chunk(b"tEXt", k.encode("latin-1") + b"\x00" + v.encode("latin-1"))

    raw = filter_scanlines(pixels, width, height, bpp, filter_)
    if backend == "storage":
        z = zlib_stream(stored_deflate(raw), raw)
    elif backend == "fixed":
        z = zlib_stream(deflate_fixed(raw), raw)
    else:
        z = zlib_compress(raw, 9)
    out += chunk(b"IDAT", z)
    out += chunk(b"IEND", b"")
    return bytes(out)

# ======================= 6. 解码自检 =======================
def decode_png(blob):
    if blob[:8] != SIG:
        raise ValueError("PNG magic 不对")
    pos, idat, ihdr, plte = 8, bytearray(), None, None
    chunks = []
    while pos < len(blob):
        ln = struct.unpack(">I", blob[pos:pos+4])[0]
        tag = blob[pos+4:pos+8]
        data = blob[pos+8:pos+8+ln]
        (crc,) = struct.unpack(">I", blob[pos+8+ln:pos+12+ln])
        if crc32(tag + data) != crc:
            raise ValueError(f"{tag} CRC 不符")
        chunks.append({"tag": tag.decode("latin-1"), "length": ln})
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            idat += data
        elif tag == b"PLTE":
            plte = data
        pos += 12 + ln
    if ihdr is None:
        raise ValueError("缺少 IHDR")
    w, h, bd, ct, comp, filt, inter = ihdr
    if comp != 0 or filt != 0 or inter != 0 or bd != 8:
        raise ValueError(f"不支持 IHDR: bitdepth={bd} comp={comp} filt={filt} inter={inter}")
    if ct not in COLOR_CHANNELS:
        raise ValueError(f"不支持的 colortype: {ct}")
    if _z is None:
        raise RuntimeError("当前运行时缺少 zlib，无法解压 PNG")
    bpp = COLOR_CHANNELS[ct]
    stride = w * bpp
    raw = _z.decompress(bytes(idat))
    if len(raw) != h * (stride + 1):
        raise ValueError(f"解压后长度 {len(raw)} != {h * (stride + 1)}")
    px = bytearray(h * stride)
    prev = bytes(stride)
    for y in range(h):
        ft = raw[y * (stride + 1)]
        line = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        cur = bytearray(stride)
        for i in range(stride):
            a = cur[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 0:
                p = 0
            elif ft == 1:
                p = a
            elif ft == 2:
                p = b
            elif ft == 3:
                p = (a + b) >> 1
            elif ft == 4:
                p = _paeth(a, b, c)
            else:
                raise ValueError(f"未知滤波类型: {ft}")
            cur[i] = (line[i] + p) & 0xFF
        px[y * stride:(y + 1) * stride] = cur
        prev = cur
    return {"w": w, "h": h, "colortype": ct, "pixels": bytes(px),
            "palette": plte, "chunks": chunks}

# ======================= 7. 样图 =======================
def demo_image(w, h):
    """画一张渐变 + 同心圆 + 棋盘的 RGBA 测试图"""
    px = bytearray(w * h * 4)
    cx, cy = w // 2, h // 2
    for y in range(h):
        for x in range(w):
            dx, dy = x - cx, y - cy
            r2 = dx * dx + dy * dy
            i = (y * w + x) * 4
            chk = 40 if ((x >> 4) ^ (y >> 4)) & 1 else 18
            px[i]     = min(255, chk + x * 255 // max(1, w - 1))
            px[i + 1] = min(255, chk + y * 255 // max(1, h - 1))
            if r2 < (cx * 0.55) ** 2:
                px[i + 2] = 255
            elif r2 < (cx * 0.85) ** 2:
                px[i + 2] = 128
            else:
                px[i + 2] = chk
            px[i + 3] = 255 if r2 < (cx * 0.9) ** 2 else 110
    return bytes(px)
