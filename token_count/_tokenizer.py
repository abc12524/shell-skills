"""纯 Python tokenizer.json 解析（BPE），不依赖 tokenizers / transformers。

仅实现本 skill 所需的编码计数：normalizer -> pre_tokenizer(Split/ByteLevel) -> BPE model。
所有词表与合并规则来自同目录 tokenizer.json。
"""
import json
import os
import re
import unicodedata

DEFAULT_TOKENIZER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokenizer.json")

# Unicode White_Space（对齐 regex crate 的 \s，避免 Python \s 的额外控制符）
_WS = ("[\\t\\n\\x0b\\x0c\\r \\x85\\xa0\\u1680\\u2000-\\u200a"
       "\\u2028\\u2029\\u202f\\u205f\\u3000]")
_NON_WS = ("[^\\t\\n\\x0b\\x0c\\r \\x85\\xa0\\u1680\\u2000-\\u200a"
           "\\u2028\\u2029\\u202f\\u205f\\u3000]")


def _ranges_for(pred):
    parts = []
    start = prev = None

    def emit(a, b):
        def cp(c):
            return "\\u%04x" % c if c <= 0xFFFF else "\\U%08x" % c
        parts.append(cp(a) if a == b else cp(a) + "-" + cp(b))

    for code in range(0x110000):
        ok = False
        if not 0xD800 <= code <= 0xDFFF:
            ok = pred(unicodedata.category(chr(code)))
        if ok:
            if start is None:
                start = prev = code
            elif code == prev + 1:
                prev = code
            else:
                emit(start, prev)
                start = prev = code
        elif start is not None:
            emit(start, prev)
            start = prev = None
    if start is not None:
        emit(start, prev)
    return "".join(parts)


_CACHE = {}


def _classes():
    if "L" not in _CACHE:
        first = {"L": "", "M": "", "N": "", "P": "", "S": ""}
        _CACHE.update(first)
        for first_letter in ("L", "M", "N", "P", "S"):
            _CACHE[first_letter] = _ranges_for(lambda cat, f=first_letter: cat[0] == f)
    return _CACHE


def _patterns():
    if "patterns" in _CACHE:
        return _CACHE["patterns"]
    c = _classes()
    num = "[" + c["N"] + "]"
    letter = c["L"]
    mark = c["M"]
    punct = c["P"]
    symbol = c["S"]
    main = (
        r'(?:[!"#$%&\'()*+,\-./:;<=>?@\[\]\\^_`{|}~][A-Za-z]+'
        r"|[^\r\n" + letter + mark + "]?[" + letter + mark + "]+"
        r"| ?[" + punct + symbol + r"]+[\r\n]*"
        r"|" + _WS + r"*[\r\n]+"
        r"|" + _WS + r"+(?!" + _NON_WS + r")"
        r"|" + _WS + r"+)"
    )
    pats = (
        re.compile(num + "{1,3}"),
        re.compile("[一-龥぀-ゟ゠-ヿ]+"),
        re.compile(main),
    )
    _CACHE["patterns"] = pats
    return pats


def _byte_encoder():
    if "byte_encoder" in _CACHE:
        return _CACHE["byte_encoder"]
    bs = (list(range(ord("!"), ord("~") + 1))
          + list(range(ord("¡"), ord("¬") + 1))
          + list(range(ord("®"), ord("ÿ") + 1)))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    enc = {b: chr(c) for b, c in zip(bs, cs)}
    _CACHE["byte_encoder"] = enc
    return enc


def _split(pattern, text):
    out = []
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            out.append(text[last:m.start()])
        out.append(m.group(0))
        last = m.end()
    if last < len(text):
        out.append(text[last:])
    return out


def _pre_tokenize(text):
    pieces = [text]
    for pat in _patterns():
        nxt = []
        for piece in pieces:
            nxt.extend(_split(pat, piece))
        pieces = nxt
    enc = _byte_encoder()
    return ["".join(enc[b] for b in piece.encode("utf-8")) for piece in pieces if piece]


def _load():
    if "data" in _CACHE:
        return _CACHE["data"]
    path = (os.environ.get("TOKEN_COUNT_TOKENIZER")
            or os.environ.get("TOKENIZER_PATH")
            or DEFAULT_TOKENIZER)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    model = data["model"]
    ranks = {}
    for i, merge in enumerate(model["merges"]):
        a, b = merge.split(" ", 1)
        ranks[(a, b)] = i
    adds = sorted((t["content"] for t in data.get("added_tokens", [])),
                  key=len, reverse=True)
    added_re = re.compile("|".join(re.escape(t) for t in adds)) if adds else None
    _CACHE["data"] = (ranks, added_re)
    return _CACHE["data"]


def _bpe(piece, ranks, cache):
    hit = cache.get(piece)
    if hit is not None:
        return hit
    symbols = list(piece)
    while len(symbols) > 1:
        best = None
        best_rank = None
        for i in range(len(symbols) - 1):
            rank = ranks.get((symbols[i], symbols[i + 1]))
            if rank is not None and (best_rank is None or rank < best_rank):
                best_rank = rank
                best = (symbols[i], symbols[i + 1])
        if best is None:
            break
        merged = []
        i = 0
        while i < len(symbols):
            if (i < len(symbols) - 1 and symbols[i] == best[0]
                    and symbols[i + 1] == best[1]):
                merged.append(best[0] + best[1])
                i += 2
            else:
                merged.append(symbols[i])
                i += 1
        symbols = merged
    count = len(symbols)
    cache[piece] = count
    return count


def count_tokens(text):
    ranks, added_re = _load()
    cache = _CACHE.setdefault("pieces", {})
    total = 0
    pos = 0
    for m in (added_re.finditer(text) if added_re else ()):
        if m.start() > pos:
            for piece in _pre_tokenize(text[pos:m.start()]):
                total += _bpe(piece, ranks, cache)
        total += 1
        pos = m.end()
    if pos < len(text):
        for piece in _pre_tokenize(text[pos:]):
            total += _bpe(piece, ranks, cache)
    return total
