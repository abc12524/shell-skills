# -*- coding: utf-8 -*-
"""抖音分享链接解析与下载（纯 Python，不需要 Node / 浏览器）。

链路：

    分享文案 -> 提取 URL -> 跟随短链 3xx -> /video/<aweme_id>
    -> /aweme/v1/web/aweme/detail/（a_bogus + x-secsdk-web-signature）
    -> 下载视频 / 图集

依赖：标准库 + requests（由 index.json 声明）；若环境里装了 curl_cffi，
会自动用它冒充 Chrome 的 TLS / HTTP2 指纹，风控更少。

认证只从环境变量读取（由 skill.py 从同目录 config.json 注入）：

    DOUYIN_COOKIE    完整 Cookie 头（必填，至少包含 UIFID）
    DOUYIN_WEBID     可选；不填则用 query/user 接口换真实 webid
    DOUYIN_MS_TOKEN  可选；不填则生成随机 msToken

指纹（必须与 Cookie 所属浏览器一致）：DOUYIN_UA / DOUYIN_SCREEN_WIDTH /
DOUYIN_SCREEN_HEIGHT / DOUYIN_CPU_CORE_NUM / DOUYIN_DEVICE_MEMORY 可覆盖默认值。
"""
from __future__ import annotations

import hashlib
import json
import os
import random as _rnd
import re
import time
import urllib.parse
from urllib.parse import urljoin, urlparse

DOUYIN_HOST = "https://www.douyin.com"
DETAIL_API = "/aweme/v1/web/aweme/detail/"
DEVICE_API = "/aweme/v1/web/query/user"
DOUYIN_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")
REDIRECT_CODES = {301, 302, 303, 307, 308}
MEDIA_HEADERS = {"Referer": "https://www.douyin.com/"}
CHUNK_SIZE = 1024 * 1024

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_TRAILING_PUNCT = "，。！？、；：）】》」』,.;!?)]}>'\""
_ID_PATTERNS = (
    re.compile(r'"aweme_id"\s*:\s*"(\d{15,25})"'),
    re.compile(r'"itemId"\s*:\s*"(\d{15,25})"'),
    re.compile(r'/(?:video|note|slides)/(\d{15,25})'),
    re.compile(r'[?&]modal_id=(\d{15,25})'),
)
_URL_ID_PATTERNS = (
    re.compile(r"/(?:video|note|slides)/(\d+)"),
    re.compile(r"[?&]modal_id=(\d+)"),
)
_ILLEGAL_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# ============================ 浏览器指纹 ============================

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
_DEFAULT_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7,ja;q=0.6"
_DEFAULT_SCREEN = ("2560", "1440")
_DEFAULT_CPU_CORE = "20"
_DEFAULT_DEVICE_MEMORY = "32"


def _env(key, default):
    return (os.getenv(key) or "").strip() or default


def profile():
    """返回与 Cookie 配套的指纹档案（进程内稳定，调用方不要修改）。"""
    ua = _env("DOUYIN_UA", _DEFAULT_UA)
    match = re.search(r"Chrome/([\d.]+)", ua)
    version = match.group(1) if match else "155.0.0.0"
    major = version.split(".")[0]
    width = _env("DOUYIN_SCREEN_WIDTH", _DEFAULT_SCREEN[0])
    height = _env("DOUYIN_SCREEN_HEIGHT", _DEFAULT_SCREEN[1])
    try:
        w, h = int(width), int(height)
    except ValueError:
        w, h = 2560, 1440
    # a_bogus 里的几何串是 w|innerH|w|outerH|w|availH|w|h|Win32
    geo = (w, h - 225, w, h - 48, w, h - 48, w, h)
    return {
        "ua": ua,
        "browser_version": version,
        "engine_version": version,
        "sec_ch_ua": (f'"Google Chrome";v="{major}", "Chromium";v="{major}", '
                      f'"Not(A:Brand";v="24"'),
        "sec_ch_ua_platform": '"Windows"',
        "browser_name": "Chrome",
        "cpu_core_num": _env("DOUYIN_CPU_CORE_NUM", _DEFAULT_CPU_CORE),
        "device_memory": _env("DOUYIN_DEVICE_MEMORY", _DEFAULT_DEVICE_MEMORY),
        "accept_language": _env("DOUYIN_ACCEPT_LANGUAGE", _DEFAULT_ACCEPT_LANGUAGE),
        "screen_width": str(w),
        "screen_height": str(h),
        "geo": geo,
    }


# ============================ SM3 ============================

_SM3_IV = [
    0x7380166f, 0x4914b2b9, 0x172442d7, 0xda8a0600,
    0xa96f30bc, 0x163138aa, 0xe38dee4d, 0xb0fb0e4e,
]


def _rotl32(x, n):
    n &= 31
    x &= 0xFFFFFFFF
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _sm3_p1(x):
    return x ^ _rotl32(x, 15) ^ _rotl32(x, 23)


def _sm3_cf(v, block):
    w = list(range(68))
    for i in range(16):
        w[i] = int.from_bytes(block[i * 4:i * 4 + 4], "big")
    for j in range(16, 68):
        w[j] = (_sm3_p1(w[j - 16] ^ w[j - 9] ^ _rotl32(w[j - 3], 15))
                ^ _rotl32(w[j - 13], 7) ^ w[j - 6]) & 0xFFFFFFFF
    w1 = [(w[j] ^ w[j + 4]) & 0xFFFFFFFF for j in range(64)]

    a, b, c, d, e, f, g, h = v
    for j in range(64):
        tj = 0x79CC4519 if j < 16 else 0x7A879D8A
        ss1 = _rotl32((_rotl32(a, 12) + e + _rotl32(tj, j)) & 0xFFFFFFFF, 7)
        ss2 = ss1 ^ _rotl32(a, 12)
        ff = (a ^ b ^ c) if j < 16 else ((a & b) | (a & c) | (b & c))
        gg = (e ^ f ^ g) if j < 16 else ((e & f) | (~e & g))
        tt1 = (ff + d + ss2 + w1[j]) & 0xFFFFFFFF
        tt2 = (gg + h + ss1 + w[j]) & 0xFFFFFFFF
        d, c, b, a = c, _rotl32(b, 9), a, tt1
        h, g, f, e = g, _rotl32(f, 19), e, (tt2 ^ _rotl32(tt2, 9) ^ _rotl32(tt2, 17)) & 0xFFFFFFFF

    return [(x ^ y) & 0xFFFFFFFF for x, y in zip([a, b, c, d, e, f, g, h], v)]


def sm3_hash(msg):
    if isinstance(msg, str):
        msg = msg.encode("utf-8")
    msg = bytearray(msg)
    bit_len = len(msg) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0x00)
    msg += bit_len.to_bytes(8, "big")
    v = _SM3_IV[:]
    for i in range(0, len(msg), 64):
        v = _sm3_cf(v, msg[i:i + 64])
    return b"".join(x.to_bytes(4, "big") for x in v)


# ============================ a_bogus ============================

SALT = "dhzx"
SDK_VERSION_CODE = 1
SDK_MINOR_CODE = 12
HOST_ABOGUS_CONFIGS = {
    # 主站 XHR 实录：L[40..42] 与 L[44..47] 与 passport 域不同
    "www.douyin.com": {"sdk_minor": 8, "l40": 132, "l41": 1, "l42": 1},
    "login.douyin.com": {"sdk_minor": 14, "l40": 0, "l41": 0, "l42": 0},
}
HOST_APP_IDS = {
    "www.douyin.com": (6383, 11881),
    "live.douyin.com": (6383, 7571),
    "creator.douyin.com": (2906, 33638),
    "login.douyin.com": (6383, 6241),
}
DEFAULT_HOST = "www.douyin.com"
FORTNIGHT_EPOCH = 1721836800000
DUMP_CLOSURE_TIME = 1720000000000
DUMP_COUNTER_INIT = 2
DUMP_BROWSER_NAME = "Firefox"
RC4_KEY_BYTE = 211
FIXED_NOW = 1720000000000
FIXED_RAND = 0.4142135623730951
ALPHABET_S3 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"
ALPHABET_S4 = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"
FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0"
GEO_PLATFORM = "Win32"
BROWSER_TABLE = (
    ("Huawei", (r"\bhuawei\b",)),
    ("Chrome", (r"(chrome)\/([\w.]+)(?!.*chromium)",)),
    ("Edge", (r"(edg|edge)\/([\w.]+)",)),
    ("Firefox", (r"\bfocus\/([\w.]+)", r"fxios\/([-\w.]+)",
                 r"mobile vr; rv:([\w.]+)\).+firefox", r"(firefox)\/([\w.]+)")),
    ("IE", (r"(msie |trident.*rv:)([\w.]+)",)),
    ("Opera", (r"(opera|opr)\/([\w.]+)",)),
    ("Safari", (r"(safari)\/([\w.]+)(?!.*chrome)",)),
)
BROWSER_OFFSET = {"Chrome": 0, "Firefox": 40, "Safari": 81, "Edge": 125, "Huawei": 170}
A98_PERM = (34, 44, 56, 61, 73, 29, 70, 45, 35, 49, 38, 66, 51, 68, 28, 48, 64, 47,
            30, 71, 26, 55, 31, 69, 59, 40, 62, 63, 27, 72, 41, 74, 57, 52, 42, 39,
            33, 67, 53, 43, 65, 46, 36, 24, 60, 32, 79, 80, 84, 85)
Z148_R_MASKS = (145, 66, 44)
Z148_IN_MASKS = (110, 189, 211)
Z146_MASK_A = 170
Z146_MASK_B = 85


def _b64_custom(data, alphabet):
    out = []
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        n = len(chunk)
        b = list(chunk) + [0] * (3 - n)
        v = (b[0] << 16) | (b[1] << 8) | b[2]
        idx = [(v >> 18) & 63, (v >> 12) & 63, (v >> 6) & 63, v & 63]
        if n == 1:
            out += [alphabet[idx[0]], alphabet[idx[1]], "=", "="]
        elif n == 2:
            out += [alphabet[idx[0]], alphabet[idx[1]], alphabet[idx[2]], "="]
        else:
            out += [alphabet[k] for k in idx]
    return "".join(out)


def _rc4_variant(key, data):
    sbox = [255 - x for x in range(256)]
    j = 0
    for i in range(256):
        j = (j * sbox[i] + j + key[i % len(key)]) % 256
        sbox[i], sbox[j] = sbox[j], sbox[i]
    out = []
    i = j = 0
    for b in data:
        i = (i + 1) % 256
        j = (j + sbox[i]) % 256
        sbox[i], sbox[j] = sbox[j], sbox[i]
        out.append(b ^ sbox[(sbox[i] + sbox[j]) % 256])
    return out


def _str_to_bytes(s):
    out = []
    for ch in s:
        c = ord(ch)
        if c & 65280:
            out.append(c >> 8)
            out.append(c & 255)
        else:
            out.append(c)
    return out


def _le_bytes(value, count):
    return [(int(value) >> (8 * i)) & 255 for i in range(count)]


def _to_int32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def _z146_blend(c0, c1, r0, r1):
    return [(r0 & Z146_MASK_A) | (c0 & Z146_MASK_B),
            (r0 & Z146_MASK_B) | (c0 & Z146_MASK_A),
            (r1 & Z146_MASK_A) | (c1 & Z146_MASK_B),
            (r1 & Z146_MASK_B) | (c1 & Z146_MASK_A)]


def _z148_expand(arr, rand_fn):
    out = []
    i = 0
    n = len(arr)
    while i < n:
        if i + 2 < n:
            r = int(rand_fn() * 1000) & 255
            b0 = (r & Z148_R_MASKS[0]) | (arr[i] & Z148_IN_MASKS[0])
            b1 = (r & Z148_R_MASKS[1]) | (arr[i + 1] & Z148_IN_MASKS[1])
            b2 = (r & Z148_R_MASKS[2]) | (arr[i + 2] & Z148_IN_MASKS[2])
            b3 = ((arr[i] & Z148_R_MASKS[0]) | (arr[i + 1] & Z148_R_MASKS[1])
                  | (arr[i + 2] & Z148_R_MASKS[2]))
            out += [b0, b1, b2, b3]
        else:
            out.append(arr[i])
            if i + 1 < n and arr[i + 1]:
                out.append(arr[i + 1])
        i += 3
    return out


def _escape_digest(digest, idx, reserved, fallback, force):
    v = digest[idx] if idx < len(digest) else fallback
    while v == reserved:
        idx += 1
        v = digest[idx] if idx < len(digest) else fallback
    return reserved if force else v


def _browser_name(ua):
    for name, regs in BROWSER_TABLE:
        if any(re.search(p, ua, re.I) for p in regs):
            return name
    return "Other"


def _z143_rand_offset(rand_fn, name):
    return int(rand_fn() * 40) + BROWSER_OFFSET.get(name, 210)


def _z144_check(rand_fn, flags_byte):
    if flags_byte & 64:
        r = int(rand_fn() * 109)
        return r + 110 + (r % 2)
    r = int(rand_fn() * 240)
    return r + (r % 2) + 1 if r > 109 else r


def _z145_permissions_bits(rand_fn):
    base = int(rand_fn() * 255) & 77
    return base | 2 | 16 | 32 | 128


def _z149_counter_bucket(counter):
    if counter > 10745:
        return 3
    if counter > 1283:
        return 4
    if counter > 139:
        return 5
    return 6


def _env_flags_byte(stack_is_node, ua):
    flags = 1
    flags |= int(stack_is_node) << 2
    flags |= int(_browser_name(ua) == "Firefox") << 5
    return flags


class ABogusSigner:
    """a_bogus 纯算法实现（主站 / 直播 / creator / passport 共用）。

    :param fixed: True 时固定时间与随机数，仅用于与抓包真值对拍。
    """

    def __init__(self, fixed=False, ua=None, *, fixed_now_ms=None, fixed_counter=None):
        self.fixed = fixed
        self.ua = ua or (FIREFOX_UA if fixed else profile()["ua"])
        self.counter = int(fixed_counter) if fixed_counter is not None else DUMP_COUNTER_INIT
        self._fixed_now_ms = None if fixed_now_ms is None else int(fixed_now_ms)
        self.geo = (2560, 1215, 2560, 1392, 2560, 1392, 2560, 1440) if fixed else profile()["geo"]
        self.offset_name = DUMP_BROWSER_NAME if fixed else _browser_name(self.ua)

    def _now(self):
        if self._fixed_now_ms is not None:
            return self._fixed_now_ms
        return FIXED_NOW if self.fixed else int(time.time() * 1000)

    def _rand(self):
        return FIXED_RAND if self.fixed else _rnd.random()

    @staticmethod
    def _host_config(host):
        return dict(HOST_ABOGUS_CONFIGS.get(host, {}))

    def sign(self, url, body="", host=None):
        parts = urllib.parse.urlsplit(url)
        return self.sign_query(parts.query, body, host=host or parts.hostname)

    def sign_query(self, query, body="", host=None):
        host = host or DEFAULT_HOST
        aid, page_id = HOST_APP_IDS.get(host, HOST_APP_IDS[DEFAULT_HOST])
        cfg = self._host_config(host)
        L = {}
        self.counter += 1
        L[12] = 3
        t = self._now()
        L[14] = t
        v129 = cfg.get("sdk_version", SDK_VERSION_CODE)
        v14 = cfg.get("sdk_minor", SDK_MINOR_CODE)
        h1 = sm3_hash(sm3_hash((query + SALT).encode("utf-8")))
        h2 = sm3_hash(sm3_hash((body + SALT).encode("utf-8")))
        ua_key = [v129 // 256, v129 % 256, v14 % 256]
        ua_cipher = _rc4_variant(ua_key, [ord(c) for c in self.ua.strip()])
        h_ua = sm3_hash(_b64_custom(ua_cipher, ALPHABET_S3).encode("utf-8"))
        ink = t - 1
        L[23] = [3, 82]
        L[24] = 41
        L[25] = [1, 0, 1, 0, 1]
        L[26] = int((t - FORTNIGHT_EPOCH) / 1000 / 60 / 60 / 24 / 14)
        L[27] = _z149_counter_bucket(self.counter)
        closure_t = DUMP_CLOSURE_TIME if self.fixed else t
        L[28] = (t - closure_t + 3) & 255 if closure_t > 0 else 2
        for i, b in enumerate(_le_bytes(t, 6)):
            L[29 + i] = b
        L[35], L[36] = _le_bytes(v129, 2)
        flags = _env_flags_byte(self.fixed, self.ua)
        L[37] = [0, 0, 0, 0, flags]
        L[38], L[39] = _le_bytes(flags, 2)
        L[40] = cfg.get("l40", 0)
        L[41] = cfg.get("l41", 0)
        L[42] = cfg.get("l42", 0)
        L[43] = 0
        for i, b in enumerate(_le_bytes(v14, 4)):
            L[44 + i] = b
        L[48], L[49] = h1[9], h1[18]
        L[51] = _escape_digest(h1, 3, 11, 12, bool(flags & 2))
        L[52], L[53] = h2[10], h2[19]
        L[55] = _escape_digest(h2, 4, 8, 9, bool(flags & 4))
        L[56], L[57] = h_ua[11], h_ua[21]
        L[59] = _escape_digest(h_ua, 5, 12, 13, bool(flags & 8))
        for i, b in enumerate(_le_bytes(ink, 6)):
            L[60 + i] = b
        L[66] = L[12]
        for i, b in enumerate(_le_bytes(page_id, 4)):
            L[67 + i] = b
        for i, b in enumerate(_le_bytes(aid, 4)):
            L[71 + i] = b
        geo_str = "|".join([str(v) for v in self.geo] + [GEO_PLATFORM])
        L[77] = _str_to_bytes(geo_str)
        L[78] = len(L[77])
        L[79], L[80] = _le_bytes(L[78], 2)
        L[81] = str((t + 3) & 255) + ","
        L[82] = _str_to_bytes(L[81])
        L[83] = len(L[82])
        L[84], L[85] = _le_bytes(L[83], 2)
        v6 = self._rand() * 65535
        a8 = _z146_blend(L[25][0], L[25][1], int(v6) & 255, (int(v6) >> 8) & 255)
        self._rand()
        a8 += _z146_blend(L[25][2], L[25][3], _z144_check(self._rand, flags),
                          _z145_permissions_bits(self._rand))
        L[86] = a8
        chk = 0
        for b in a8 + [L[s] for s in (
                24, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 38, 39, 40, 41, 42, 43,
                44, 45, 46, 47, 48, 49, 51, 52, 53, 55, 56, 57, 59, 60, 61, 62, 63, 64,
                65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 79, 80, 84, 85)]:
            chk ^= b
        L[87] = _to_int32(chk)
        a98 = [L[s] for s in A98_PERM] + L[77] + L[82] + [L[87]]
        L[88] = a98
        hr0 = int(self._rand() * 65535) & 255
        L[89] = _z146_blend(3, 82, hr0, _z143_rand_offset(self._rand, self.offset_name))
        plain = a8 + _z148_expand(a98, self._rand)
        cipher = _rc4_variant([RC4_KEY_BYTE], [x & 0xFFFF for x in plain])
        return _b64_custom(L[89] + [c & 255 for c in cipher], ALPHABET_S4)


_signer = None


def generate_a_bogus(query, data="", host=DEFAULT_HOST):
    global _signer
    if _signer is None:
        _signer = ABogusSigner(fixed=False)
    return _signer.sign_query(query, data, host=host)


# ============================ x-secsdk-web-signature ============================

WEBSIGN_CONST = "A96D855A08C0A9707F8BEF0D9A527E4E"
_ENCODE_SAFE = "!*'()"
_SIG_KEY = "x-secsdk-web-signature"
_TS_KEY = "timestamp"


def _encode_component(value):
    return urllib.parse.quote(str(value), safe=_ENCODE_SAFE, encoding="utf-8")


def canonical_query(query):
    parts = []
    for pair in query.split("&"):
        if not pair:
            continue
        if "=" in pair:
            key, value = pair.split("=", 1)
        else:
            key, value = pair, ""
        key = urllib.parse.unquote_plus(key, encoding="utf-8", errors="replace")
        value = urllib.parse.unquote_plus(value, encoding="utf-8", errors="replace")
        parts.append("%s=%s" % (key, _encode_component(value)))
    return "&".join(parts)


def _split_signed(url):
    base, _, query = url.partition("?")
    kept = []
    for pair in query.split("&"):
        if not pair:
            continue
        name = pair.split("=", 1)[0]
        if name in (_TS_KEY, _SIG_KEY):
            continue
        kept.append(pair)
    return base, "&".join(kept)


def web_sign(url, ts=None, uifid=None):
    """返回 (timestamp, signature, signed_query)。"""
    if ts is None:
        ts = int(time.time())
    ts = int(ts)
    _, raw_query = _split_signed(url)
    canon = canonical_query(raw_query)
    names = [p.split("=", 1)[0] for p in canon.split("&") if p]
    if "uifid" not in names and uifid:
        canon = ("%s&uifid=%s" % (canon, _encode_component(uifid))
                 if canon else "uifid=%s" % _encode_component(uifid))
    signed_query = ("%s&%s=%d" % (canon, _TS_KEY, ts)) if canon \
        else "%s=%d" % (_TS_KEY, ts)
    uifid_value = ""
    for pair in signed_query.split("&"):
        if pair.startswith("uifid="):
            uifid_value = urllib.parse.unquote_plus(pair[6:], encoding="utf-8",
                                                    errors="replace")
            break
    if not uifid_value and uifid:
        uifid_value = uifid
    plain = "%s_%d_%s_%s" % (uifid_value, ts, WEBSIGN_CONST, signed_query)
    return ts, hashlib.md5(plain.encode("utf-8")).hexdigest(), signed_query


def sign_url(url, ts=None, uifid=None):
    base, _ = _split_signed(url)
    ts, signature, signed_query = web_sign(url, ts=ts, uifid=uifid)
    return "%s?%s&%s=%s" % (base, signed_query, _SIG_KEY, signature)


# ============================ query / header ============================


def _splice_url(params):
    parts = []
    for key, value in params.items():
        if value is None:
            value = ""
        parts.append(key + "=" + urllib.parse.quote(str(value), safe=""))
    return "&".join(parts)


class Params:
    def __init__(self):
        self.params = {}

    def add(self, key, value):
        self.params[key] = value
        return self

    def with_platform(self, round_trip_time="0", version_code="170400",
                      version_name="17.4.0"):
        fp = profile()
        self.params.update({
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "update_version_code": "170400",
            "pc_client_type": "1",
            "pc_libra_divert": "Windows",
            "support_h265": "1",
            "support_dash": "1",
            "cpu_core_num": fp["cpu_core_num"],
            "version_code": version_code,
            "version_name": version_name,
            "cookie_enabled": "true",
            "screen_width": fp["screen_width"],
            "screen_height": fp["screen_height"],
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": fp["browser_name"],
            "browser_version": fp["browser_version"],
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": fp["engine_version"],
            "os_name": "Windows",
            "os_version": "10",
            "device_memory": fp["device_memory"],
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": round_trip_time,
        })
        return self

    def with_a_bogus(self, host="www.douyin.com"):
        self.add("a_bogus", generate_a_bogus(_splice_url(self.params), "", host=host))
        return self

    def signed_url(self, base_url, uifid=None):
        return sign_url(base_url + "?" + self.to_string(), uifid=uifid)

    def to_string(self):
        return "&".join("%s=%s" % (k, v) for k, v in self.params.items())

    def get(self):
        return self.params


def doc_headers(cookie=""):
    fp = profile()
    headers = {
        "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8,"
                   "application/signed-exchange;v=b3;q=0.7"),
        "accept-language": fp["accept_language"],
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "priority": "u=0, i",
        "sec-ch-ua": fp["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": fp["sec_ch_ua_platform"],
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": fp["ua"],
    }
    if cookie:
        headers["cookie"] = cookie
    return headers


def xhr_headers(referer, uifid, cookie):
    fp = profile()
    return {
        "user-agent": fp["ua"],
        "accept": "application/json, text/plain, */*",
        "sec-ch-ua": fp["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": fp["sec_ch_ua_platform"],
        "accept-language": fp["accept_language"],
        "priority": "u=1, i",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "referer": referer,
        "uifid": uifid,
        "cookie": cookie,
    }


# ============================ HTTP ============================

_curl = None
_requests = None
_warnings_off = False


def _get_curl():
    global _curl
    if _curl is None:
        try:
            from curl_cffi import requests as curl_requests
            _curl = curl_requests
        except Exception:
            _curl = False
    return _curl or None


def _get_requests():
    global _requests, _warnings_off
    if _requests is None:
        import requests
        _requests = requests
    if not _warnings_off:
        _warnings_off = True
        try:
            _requests.packages.urllib3.disable_warnings()
        except Exception:
            pass
    return _requests


def _impersonate():
    try:
        from curl_cffi.requests.impersonate import BrowserType
        supported = {item.value for item in BrowserType}
    except Exception:
        supported = set()
    if not supported:
        return "chrome151"
    for name in ("chrome151", "chrome150"):
        if name in supported:
            return name
    return "chrome" if "chrome" in supported else None


_IMPERSONATE = None


def http_get(url, headers=None, timeout=20, allow_redirects=True, stream=False):
    """GET 请求；优先 curl_cffi（Chrome TLS 指纹），否则退回 requests。"""
    global _IMPERSONATE
    curl = _get_curl()
    if curl is not None:
        if _IMPERSONATE is None:
            _IMPERSONATE = _impersonate()
        kwargs = {
            "headers": headers, "timeout": timeout, "verify": False,
            "allow_redirects": allow_redirects, "stream": stream,
            "default_headers": False,
        }
        if _IMPERSONATE:
            kwargs["impersonate"] = _IMPERSONATE
        return curl.get(url, **kwargs)
    return _get_requests().get(
        url, headers=headers, timeout=timeout, verify=False,
        allow_redirects=allow_redirects, stream=stream,
    )


def _header(response, name):
    headers = getattr(response, "headers", None) or {}
    wanted = name.lower()
    try:
        for key, value in dict(headers).items():
            if str(key).lower() == wanted and value:
                return str(value)
    except (TypeError, ValueError):
        return ""
    return ""


def _response_text(response):
    text = getattr(response, "text", None)
    if text is None:
        content = getattr(response, "content", b"") or b""
        text = content.decode("utf-8", "replace")
    return text or ""


def _response_json(response):
    try:
        return response.json()
    except Exception:
        text = _response_text(response)
        return json.loads(text)


def _iter_content(response):
    yield from response.iter_content(chunk_size=CHUNK_SIZE)


# ============================ 分享链接解析 ============================


def _is_douyin_host(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in DOUYIN_HOST_SUFFIXES)


def extract_share_url(text):
    """从分享文案里取出抖音链接；有多条时优先 v.douyin.com 短链。"""
    candidates = []
    for raw in _URL_RE.findall(str(text or "")):
        url = raw.rstrip(_TRAILING_PUNCT)
        if _is_douyin_host(url):
            candidates.append(url)
    if not candidates:
        raise ValueError("分享内容里没有找到抖音链接")
    for url in candidates:
        if (urlparse(url).hostname or "").lower() == "v.douyin.com":
            return url
    return candidates[0]


def aweme_id_from_url(url):
    for pattern in _URL_ID_PATTERNS:
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return ""


def aweme_id_from_text(text):
    text = str(text or "").replace("\\/", "/").replace('\\"', '"')
    for pattern in _ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def resolve_share_url(share_url, fetch=None, max_hops=6, on_hop=None):
    """跟随短链 3xx 并归一化成 https://www.douyin.com/video/<id>。"""
    fetch = fetch or (lambda url: http_get(
        url, headers=doc_headers(), timeout=20, allow_redirects=False))
    url = share_url
    response = None
    for hop in range(max_hops):
        if on_hop is not None:
            on_hop(hop, url)
        response = fetch(url)
        location = _header(response, "location")
        if getattr(response, "status_code", 0) in REDIRECT_CODES and location:
            next_url = urljoin(url, location)
            if not _is_douyin_host(next_url):
                raise ValueError(f"重定向到了非抖音域名，已停止: {next_url}")
            url = next_url
            continue
        break
    aweme_id = aweme_id_from_url(url) or aweme_id_from_text(_response_text(response))
    if not aweme_id:
        raise ValueError(
            "重定向后仍未拿到 aweme_id；可能是短链过期、被风控，或分享页只返回了壳。"
            f" 最后 URL: {url}"
        )
    return f"{DOUYIN_HOST}/video/{aweme_id}"


def parse_aweme_id(url):
    aweme_id = aweme_id_from_url(url)
    if not aweme_id:
        raise ValueError(f"无法从链接中解析 aweme_id: {url}")
    return aweme_id, f"{DOUYIN_HOST}/video/{aweme_id}"


# ============================ 详情客户端 ============================


def _trans_cookies(cookie_str):
    cookies = {}
    for item in (cookie_str or "").split(";"):
        name, sep, value = item.strip().partition("=")
        if name and sep:
            cookies[name] = value
    return cookies


class DouyinClient:
    """只读客户端：Cookie 由环境变量提供，不需要 Node / 登录链。"""

    def __init__(self, cookie=None, *, webid=None, ms_token=None, timeout=20):
        self.cookie_str = (cookie or os.getenv("DOUYIN_COOKIE") or "").strip()
        if not self.cookie_str:
            raise RuntimeError(
                "缺少 DOUYIN_COOKIE。请在浏览器打开 www.douyin.com，从任意 "
                "/aweme/v1/web/ 请求复制完整 Cookie（必须含 UIFID），写入技能的 "
                "config.json。"
            )
        self.cookies = _trans_cookies(self.cookie_str)
        self.timeout = timeout
        self.uifid = (os.getenv("DOUYIN_UIFID") or "").strip() or self.cookies.get("UIFID", "")
        if not self.uifid:
            raise RuntimeError("Cookie 里没有 UIFID，请重新复制完整 Cookie。")
        if self.cookies.get("UIFID") != self.uifid:
            self.cookies["UIFID"] = self.uifid
            self.cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        self.ms_token = (ms_token or os.getenv("DOUYIN_MS_TOKEN") or "").strip() \
            or generate_ms_token()
        self.webid = (webid or os.getenv("DOUYIN_WEBID") or "").strip() \
            or self._resolve_webid()
        if not self.webid:
            raise RuntimeError(
                "无法获取真实 webid。请从浏览器请求里复制 webid 参数，设置 "
                "DOUYIN_WEBID 后重试。"
            )

    def _request_json(self, path, params, referer):
        url = params.signed_url(f"{DOUYIN_HOST}{path}", uifid=self.uifid)
        response = http_get(url, headers=xhr_headers(referer, self.uifid, self.cookie_str),
                            timeout=self.timeout)
        text = _response_text(response).strip()
        logid = _header(response, "x-tt-logid")
        if not text:
            raise RuntimeError(
                f"{path} 返回空响应（HTTP {getattr(response, 'status_code', '?')}）。"
                f"通常是 Cookie/UIFID/webid 失效或被风控。logid={logid}"
            )
        if not text.startswith(("{", "[")):
            raise RuntimeError(
                f"{path} 返回非 JSON（HTTP {getattr(response, 'status_code', '?')}）："
                f"{text[:120]!r} logid={logid}"
            )
        return json.loads(text)

    def _resolve_webid(self):
        params = Params()
        params.with_platform()
        params.add("publish_video_strategy_type", "2")
        params.add("webid", generate_fake_webid())
        params.add("uifid", self.uifid)
        fp = self.cookies.get("s_v_web_id", "")
        if fp:
            params.add("verifyFp", fp)
            params.add("fp", fp)
        params.add("msToken", self.ms_token)
        params.with_a_bogus()
        try:
            data = self._request_json(DEVICE_API, params, f"{DOUYIN_HOST}/discover")
            webid = str(data.get("id") or "")
            if webid:
                return webid
        except Exception:
            pass
        return self._resolve_webid_from_page()

    def _resolve_webid_from_page(self):
        try:
            response = http_get(f"{DOUYIN_HOST}/discover",
                                headers=doc_headers(self.cookie_str),
                                timeout=self.timeout)
            match = re.search(r'\\?"user_unique_id\\?"\s*:\s*\\?"(\d+)',
                              _response_text(response))
            return match.group(1) if match else ""
        except Exception:
            return ""

    def get_work_info(self, url):
        aweme_id, referer = parse_aweme_id(url)
        params = Params()
        params.add("device_platform", "webapp")
        params.add("aid", "6383")
        params.add("channel", "channel_pc_web")
        params.add("aweme_id", aweme_id)
        params.add("request_source", "600")
        params.add("origin_type", "video_page")
        params.with_platform(round_trip_time="50", version_code="190500",
                             version_name="19.5.0")
        params.add("webid", self.webid)
        params.add("uifid", self.uifid)
        fp = self.cookies.get("s_v_web_id", "")
        if fp:
            params.add("verifyFp", fp)
            params.add("fp", fp)
        params.add("msToken", self.ms_token)
        params.with_a_bogus()
        return self._request_json(DETAIL_API, params, referer)


def generate_ms_token(random_length=107):
    base_str = "ABCDEFGHIGKLMNOPQRSTUVWXYZabcdefghigklmnopqrstuvwxyz0123456789="
    length = len(base_str) - 1
    return "".join(base_str[_rnd.randint(0, length)] for _ in range(random_length))


def generate_fake_webid(random_length=19):
    base_str = "0123456789"
    length = len(base_str) - 1
    return "".join(base_str[_rnd.randint(0, length)] for _ in range(random_length))


# ============================ 作品信息 ============================


def _pick_image_url(item):
    """图集条目可能是字符串，也可能是带 url_list 的 dict。"""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    urls = [url for url in (item.get("url_list") or []) if isinstance(url, str)]
    for url in urls:
        path = url.split("?", 1)[0].lower()
        if path.endswith((".jpeg", ".jpg")):
            return url
    return urls[0] if urls else ""


def normalize_images(work):
    """把 aweme_detail 里带 url_list 的图集对象摊平成 URL 列表。"""
    images = []
    for item in work.get("images") or []:
        url = _pick_image_url(item)
        if url and url not in images:
            images.append(url)
    work["images"] = images
    return work


def _first_url(container):
    if not isinstance(container, dict):
        return ""
    for url in container.get("url_list") or []:
        if isinstance(url, str):
            return url
    return ""


def handle_work_info(data):
    """把 aweme_detail 拍平成下载 / 展示需要的字段。"""
    author = data.get("author") or {}
    stats = data.get("statistics") or {}
    video = data.get("video") or {}
    aweme_id = str(data.get("aweme_id") or "")
    gender = author.get("gender")
    gender = "男" if gender == 1 else ("女" if gender == 0 else "未知")
    try:
        ip_location = data["user"]["ip_location"]
    except (KeyError, TypeError):
        ip_location = "未知"
    topics = []
    for item in data.get("text_extra") or []:
        name = item.get("hashtag_name") if isinstance(item, dict) else ""
        if name:
            topics.append(name)
    aweme_type = data.get("aweme_type")
    work_type = "图集" if aweme_type == 68 else ("视频" if aweme_type == 0 else "未知")
    if work_type == "未知" and data.get("images"):
        work_type = "图集"
    return normalize_images({
        "work_id": aweme_id,
        "work_url": f"{DOUYIN_HOST}/video/{aweme_id}",
        "work_type": work_type,
        "title": data.get("desc") or "",
        "desc": data.get("desc") or "",
        "admire_count": stats.get("admire_count", 0),
        "digg_count": stats.get("digg_count", 0),
        "comment_count": stats.get("comment_count", 0),
        "collect_count": stats.get("collect_count", 0),
        "share_count": stats.get("share_count", 0),
        "video_addr": _first_url(video.get("play_addr")),
        "video_cover": _first_url(video.get("cover")),
        "images": data.get("images") or [],
        "topics": topics,
        "create_time": data.get("create_time") or 0,
        "user_url": f"{DOUYIN_HOST}/user/{author.get('sec_uid', '')}",
        "user_id": author.get("unique_id", "未知"),
        "nickname": author.get("nickname") or "",
        "author_avatar": _first_url(author.get("avatar_thumb")),
        "user_desc": author.get("signature", "未知"),
        "following_count": author.get("following_count", "未知"),
        "follower_count": author.get("follower_count", "未知"),
        "total_favorited": author.get("total_favorited", "未知"),
        "aweme_count": author.get("aweme_count", "未知"),
        "user_age": author.get("user_age", "未知"),
        "gender": gender,
        "ip_location": ip_location,
    })


# ============================ 画质 ============================


def video_variants(detail):
    """把 aweme_detail.video.bit_rate 摊平成可比较的画质列表。"""
    video = (detail or {}).get("video") or {}
    variants = []
    for item in video.get("bit_rate") or []:
        play = item.get("play_addr") or {}
        urls = [url for url in (play.get("url_list") or []) if isinstance(url, str)]
        try:
            width = int(play.get("width") or item.get("width") or 0)
            height = int(play.get("height") or item.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if not urls or not width or not height:
            continue
        variants.append({
            "width": width,
            "height": height,
            "short": min(width, height),
            "pixels": width * height,
            "codec": "h265" if item.get("is_h265") else "h264",
            "bitrate": int(item.get("bit_rate") or 0),
            "size": int(play.get("data_size") or 0),
            "url": urls[0],
            "gear": str(item.get("gear_name") or ""),
        })
    best_by_key = {}
    for variant in variants:
        key = (variant["width"], variant["height"], variant["codec"])
        current = best_by_key.get(key)
        if current is None or variant["bitrate"] > current["bitrate"]:
            best_by_key[key] = variant
    return sorted(best_by_key.values(),
                  key=lambda v: (v["pixels"], v["bitrate"]), reverse=True)


def choose_video_variant(variants, quality):
    """按 best / h264 / 2160 / 1440 / 1080 / 720 / 540 选一个档位。"""
    if not variants:
        return None
    quality = str(quality or "best").strip().lower()
    if quality in ("best", "max"):
        return max(variants, key=lambda v: (v["pixels"], v["bitrate"]))
    if quality in ("h264", "avc"):
        pool = [v for v in variants if v["codec"] == "h264"]
        return max(pool or variants, key=lambda v: (v["pixels"], v["bitrate"]))
    if not quality.isdigit():
        raise ValueError("quality 只支持 best / h264 / 2160 / 1440 / 1080 / 720 / 540")
    target = int(quality)
    pool = [v for v in variants if abs(v["short"] - target) <= max(1, target * 0.1)]
    if not pool:
        pool = variants
    return max(pool, key=lambda v: (v["bitrate"], v["pixels"]))


def apply_quality(detail, work, quality):
    """视频作品按所选画质改写 work['video_addr']；图集原样返回。"""
    if work.get("work_type") != "视频":
        return work, None
    variant = choose_video_variant(video_variants(detail), quality)
    if variant is None:
        return work, None
    work["video_addr"] = variant["url"]
    return work, variant


# ============================ 下载 ============================


def safe_name(text, limit):
    name = _ILLEGAL_NAME_RE.sub("", str(text or ""))
    name = name.replace("\n", "").replace("\r", "").strip().strip(".")
    return name[:limit]


def _download_media(path, name, url, kind, timeout, attempts=3):
    headers = dict(MEDIA_HEADERS)
    headers["user-agent"] = profile()["ua"]
    suffix = ".mp4" if kind == "video" else ".jpg"
    target = os.path.join(path, name + suffix)
    last_error = None
    for attempt in range(attempts):
        response = None
        try:
            response = http_get(url, headers=headers, timeout=timeout,
                                stream=(kind == "video"))
            status = getattr(response, "status_code", 200)
            if status >= 400:
                raise RuntimeError(f"下载失败 HTTP {status}: {url}")
            if kind == "video":
                with open(target, "wb") as f:
                    for chunk in _iter_content(response):
                        if chunk:
                            f.write(chunk)
            else:
                with open(target, "wb") as f:
                    f.write(getattr(response, "content", b"") or b"")
            return target
        except Exception as exc:  # noqa: BLE001 - CDN 偶发失败重试
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1)
        finally:
            try:
                response.close()
            except Exception:
                pass
    raise RuntimeError(f"下载失败: {last_error}")


def download_work(work, out_dir, media="auto", timeout=20):
    """保存到 <out>/<昵称>_<用户id>/<作品id>/，返回文件列表。"""
    work_id = work.get("work_id") or ""
    user_id = safe_name(work.get("user_id") or "", 40)
    nickname = safe_name(work.get("nickname") or "", 20) or "未知作者"
    work_type = work.get("work_type") or "未知"
    folder = os.path.join(out_dir, f"{nickname}_{user_id}", str(work_id))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "info.json"), "w", encoding="utf-8") as f:
        json.dump(work, f, ensure_ascii=False, indent=2)
    files = []
    want_video = media in ("video", "all") or (media == "auto" and work_type != "图集")
    want_image = media in ("image", "all") or (media == "auto" and work_type == "图集")
    if want_image and work_type == "图集":
        for index, url in enumerate(work.get("images") or []):
            files.append(_download_media(folder, f"image_{index}", url, "image", timeout))
    elif want_video:
        if work.get("video_cover"):
            files.append(_download_media(folder, "cover", work["video_cover"],
                                         "image", timeout))
        if work.get("video_addr"):
            files.append(_download_media(folder, "video", work["video_addr"],
                                         "video", timeout))
    return folder, files
