"""编辑层：行/区间级改写原语（替换、插入、删除、缩进与空行、diff 落盘）。

只操作行列表与字符串，不做语法解析；定位结果由上层（script_editor）传进来。
"""

import difflib
from math import gcd
import os
import re

from typing import List

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

from ._text import _join_lines, _make_diff, _new_lines, _short_path, _split_lines, write_file

from ._symbols import SymbolRange


def _auto_indent(new_code: str, indent: str) -> str:
    """new_code 首行无缩进且目标上下文有缩进时，整体补齐缩进"""
    if not indent or not new_code:
        return new_code
    rows = new_code.split("\n")
    first = next((r for r in rows if r.strip()), "")
    if first[:1] in (" ", "\t"):
        return new_code
    return "\n".join(indent + r if r.strip() else r for r in rows)


# `@n:` 相对缩进标记：n 为相对层数（可为负），工具换算成实际缩进，省掉手数空格
_REL_INDENT_RE = re.compile(r"^@(-?\d+): ?")


def _has_rel_indents(text: str) -> bool:
    return any(_REL_INDENT_RE.match(line) for line in (text or "").split("\n"))


def _indent_unit(lines: List[str]) -> str:
    """探测文件缩进单位：出现 tab 缩进即用 tab，否则取各级缩进宽度的公约数（默认 4 空格）"""
    widths = set()
    for line in lines:
        body = line.lstrip(" \t")
        if not body:
            continue
        prefix = line[:len(line) - len(body)]
        if "\t" in prefix:
            return "\t"
        if prefix:
            widths.add(len(prefix))
    if not widths:
        return "    "
    w = 0
    for width in widths:
        w = gcd(w, width)
    return " " * (w if w else 4)


def _apply_rel_indents(new_code: str, base: str, unit: str) -> str:
    """把 `@n:` 换算成 base + n*unit；无标记的行原样保留（视为已对齐到 base）。

    base 是插入点的基准缩进（add 到符号体时为体缩进），因此 @0 = 基准层、@1 = 深一层。
    标记后的内容会原样拼接，再写字面缩进属于额外缩进（会叠加）；层级请统一用 @n 表达。
    """
    out = []
    for line in (new_code or "").split("\n"):
        m = _REL_INDENT_RE.match(line)
        if not m:
            out.append(line)
            continue
        level = int(m.group(1))
        rest = line[m.end():]
        if level < 0:                       # 负层数：从 base 里退，退到底为止
            prefix = base[:max(0, len(base) + level * len(unit))]
        else:
            prefix = base + unit * level
        out.append(prefix + rest if rest.strip() else "")
    return "\n".join(out)


def _prepare(new_code: str, indent: str, unit: str = "    ") -> str:
    """统一缩进入口：带 `@n:` 标记按相对层级换算，否则沿用整体补齐"""
    if _has_rel_indents(new_code):
        return _apply_rel_indents(new_code, indent or "", unit or "    ")
    return _auto_indent(new_code, indent)


# 定义/声明类语句的首行特征：命中说明新内容是一个新的「块」，插入时宜与上文空行分隔


_DEF_LIKE_RE = re.compile(
    r"^(?:@|async\s+def\b|def\b|class\b|export\s|function\b|interface\b|enum\b|struct\b|impl\b|"
    r"fn\b|func\b|namespace\b|type\b|public\b|private\b|protected\b|static\b|abstract\b|"
    r"sub\b|[A-Za-z_]\w*\s*\(\s*\)\s*\{)")


def _def_like(text: str) -> bool:
    """new_code 的首个非空行是否是定义/声明类语句（`@n:` 前缀先剥掉）"""
    for line in (text or "").split("\n"):
        s = _REL_INDENT_RE.sub("", line.strip())
        if s:
            return bool(_DEF_LIKE_RE.match(s))
    return False


def _pad_blank(lines: List[str], at: int, text: str, next_is_def: bool = False) -> str:
    """按需给插入内容补空行；`at` 语义同 _insert_at_line（插到第 at 行之后，0 = 文件开头）。

    - 新内容以 def/class/function 等定义开头、且插入点上一行不是空行 -> 前面补 1 个空行；
    - next_is_def=true（后面紧挨着一个定义）-> 末尾补到恰有 1 个空行；
    只调整插入内容自身，不改动文件其它部分。
    """
    if not text.strip():
        return text
    prev = lines[at - 1] if 1 <= at <= len(lines) else ""
    if _def_like(text) and prev.strip() and not text.startswith("\n"):
        text = "\n" + text
    if next_is_def and not text.endswith("\n\n"):
        text = text.rstrip("\n") + "\n\n"
    return text


def _body_indent(lines: List[str], sym: SymbolRange) -> str:
    """取符号体的缩进（首个非空体行）；无体则退化为符号缩进 + 4 空格"""
    for i in range(sym.body_start, min(sym.body_end, len(lines)) + 1):
        if 1 <= i <= len(lines) and lines[i - 1].strip():
            return re.match(r"[ \t]*", lines[i - 1]).group(0)
    if sym.body_start == sym.start and sym.body_end == sym.end:
        return sym.indent + "    " if sym.kind in ("class", "function", "method") else sym.indent
    return sym.indent


def _closest_lines(code: str, old_code: str, top: int = 3):
    """old_code 未命中时，给出最接近的行窗口（带行号），便于二次精确命中"""
    lines = code.splitlines()
    want = old_code.splitlines()
    if not want or not lines:
        return []
    n = len(want)
    first = want[0].strip()
    idxs = set()
    for hit in difflib.get_close_matches(first, [l.strip() for l in lines], n=8, cutoff=0.5):
        for i, l in enumerate(lines):
            if l.strip() == hit:
                idxs.add(i)
    if not idxs:
        idxs = set(range(min(len(lines), 200)))
    scored = []
    for i in sorted(idxs):
        window = "\n".join(lines[i:i + n])
        r = difflib.SequenceMatcher(None, old_code, window).ratio()
        scored.append((r, i + 1, min(i + n, len(lines)), window))
    scored.sort(key=lambda x: -x[0])
    out = []
    for r, s, e, window in scored[:top]:
        if r < 0.4:
            continue
        text = window if len(window) <= 300 else window[:300] + "…"
        out.append({"lines": str(s) if s == e else f"{s}-{e}", "ratio": round(r, 2), "text": text})
    return out


_NOT_FOUND_MSG = "old_code 未找到（已按精确、忽略缩进、忽略标点三级匹配）"

# 标点归一表：只统一「书写风格」类差异（引号/反引号/语句分隔符/路径斜杠）
_NORM_TRANS = str.maketrans({"'": '"', "`": '"', ";": ",", "\\": "/"})


def _line_key(line: str, level: str) -> str:
    """匹配用行指纹：stripped 去首尾空白；normalized 再统一引号/分隔符并压缩空白。

    normalized 不动运算符与标识符 —— `x + 2` 与 `x * 2` 不会被当成同一行（否则会改错代码）。
    """
    text = line.strip()
    if level == "stripped":
        return text
    return re.sub(r"\s+", " ", text.translate(_NORM_TRANS))


def _fuzzy_hits(lines: List[str], old_code: str):
    """逐行模糊匹配，返回 (level, [(起始下标, 结束下标)])；先 stripped 后 normalized"""
    want = (old_code or "").split("\n")
    while want and not want[0].strip():
        want.pop(0)
    while want and not want[-1].strip():
        want.pop()
    n = len(want)
    if not n:
        return None, []
    for level in ("stripped", "normalized"):
        keys = [_line_key(w, level) for w in want]
        hits, last = [], -1
        for i in range(len(lines) - n + 1):
            if i <= last:                       # 跳过与前一处重叠的窗口
                continue
            if all(_line_key(lines[i + k], level) == keys[k] for k in range(n)):
                hits.append((i, i + n - 1))
                last = i + n - 1
        if hits:
            return level, hits
    return None, []


def _multi_msg_lines(hits, level: str) -> str:
    rows = ", ".join(str(i + 1) for i, _ in hits[:20])
    why = "忽略缩进" if level == "stripped" else "忽略标点"
    return (f"old_code 匹配到 {len(hits)} 处（行 {rows}，{why}），无法确定目标；"
            f"请加长片段，或设 replace_all=true 全部替换")


def _reindent(text: str, base_line: str, ref: str, unit: str = "    ") -> str:
    """把 text 的缩进平移到 base_line 的实际缩进（相对 ref 的缩进）；带 `@n:` 时按标记换算"""
    actual = re.match(r"[ \t]*", base_line).group(0)
    if _has_rel_indents(text):
        return _apply_rel_indents(text, actual, unit or "    ")
    first = next((ln for ln in (ref or "").split("\n") if ln.strip()), "")
    ref_indent = re.match(r"[ \t]*", first).group(0)
    out = []
    for line in (text or "").split("\n"):
        if not line.strip():
            out.append("")
            continue
        k = 0
        while k < len(ref_indent) and k < len(line) and line[k] in " \t":
            k += 1                          # 去掉 new_code 里原有的那一层基准缩进
        out.append(actual + line[k:])
    return "\n".join(out)


def _fuzzy_replace(code: str, old_code: str, new_code: str, replace_all: bool, unit: str):
    """精确未命中时的按行替换，返回 (new_src, count, level, err, err_code)"""
    lines = code.split("\n")
    level, hits = _fuzzy_hits(lines, old_code)
    if not hits:
        return None, 0, None, _NOT_FOUND_MSG, "not_found"
    if len(hits) > 1 and not replace_all:
        return None, len(hits), level, _multi_msg_lines(hits, level), "multiple_matches"
    picked = hits if replace_all else hits[:1]
    out, pos = [], 0
    for i, j in picked:
        out.extend(lines[pos:i])
        out.extend(_reindent(new_code, lines[i], old_code, unit).split("\n"))
        pos = j + 1
    out.extend(lines[pos:])
    return "\n".join(out), len(picked), level, None, None


def _fuzzy_add(code: str, old_code: str, new_code: str, replace_all: bool, before: bool, unit: str):
    """精确未命中时的按行插入，返回 (new_src, count, level, err, err_code)"""
    lines = code.split("\n")
    level, hits = _fuzzy_hits(lines, old_code)
    if not hits:
        return None, 0, None, _NOT_FOUND_MSG, "not_found"
    if len(hits) > 1 and not replace_all:
        return None, len(hits), level, _multi_msg_lines(hits, level), "multiple_matches"
    picked = hits if replace_all else hits[:1]
    out, pos = [], 0
    for i, j in picked:
        block = _reindent(new_code, lines[i], old_code, unit).split("\n")
        if before:
            out.extend(lines[pos:i])
            out.extend(block)
            pos = i
        else:
            out.extend(lines[pos:j + 1])
            out.extend(block)
            pos = j + 1
    out.extend(lines[pos:])
    return "\n".join(out), len(picked), level, None, None


def _replace(code: str, old_code: str, new_code: str, replace_all: bool, unit: str = "    "):
    """替换：先精确匹配，未命中再按行模糊匹配（忽略缩进 -> 忽略标点）。

    返回 (new_src, count, err, err_code, meta)；meta 形如 {"matched": "stripped"}，精确命中为 None。
    """
    count = code.count(old_code)
    if count:
        body = _prepare(new_code, "", unit)      # 精确路径：new_code 以行首为基准
        if count > 1 and not replace_all:
            return None, count, _multi_msg(code, old_code, count), "multiple_matches", None
        src = code.replace(old_code, body) if replace_all else code.replace(old_code, body, 1)
        return src, count if replace_all else 1, None, None, None
    new_src, count, level, err, ecode = _fuzzy_replace(code, old_code, new_code, replace_all, unit)
    return new_src, count, err, ecode, ({"matched": level} if level else None)


def _multi_msg(code: str, old_code: str, count: int) -> str:
    hits: List[int] = []
    start = 0
    for _ in range(count):
        idx = code.find(old_code, start)
        hits.append(code.count("\n", 0, idx) + 1)
        start = idx + len(old_code)
    return (f"old_code 匹配到 {count} 处（行 {', '.join(map(str, hits[:20]))}），无法确定目标；"
            f"请加长片段，或设 replace_all=true 全部替换")


def _replace_lines(code: str, start, end, new_code: str):
    """按行号替换 [start, end]（1-based，含端点）为 new_code"""
    lines, trailing = _split_lines(code)
    total = len(lines)
    if start is None or start < 1 or start > total:
        return None, f"起始行需在 1-{total}: {start}"
    if end is None:
        end = start
    if end < start or end > total:
        return None, f"结束行需在 {start}-{total}: {end}"
    lines[start - 1:end] = _new_lines(new_code)
    return _join_lines(lines, trailing), None


def _insert_at_line(code: str, at: int, new_code: str):
    """在第 at 行之后插入 new_code（0 = 文件开头）"""
    lines, trailing = _split_lines(code)
    total = len(lines)
    if at < 0 or at > total:
        return None, f"插入位置需在 0-{total}: {at}"
    lines[at:at] = _new_lines(new_code)
    return _join_lines(lines, max(trailing, bool(_new_lines(new_code)))), None


def _add_after(code: str, anchor: str, new_code: str, replace_all: bool, before: bool = False,
               unit: str = "    "):
    """在 anchor 之后（或之前）按行边界插入 new_code。

    返回 (new_src, count, err, err_code, meta)；精确未命中时退化为按行模糊匹配。
    """
    count = code.count(anchor)
    if count == 0:
        new_src, count, level, err, ecode = _fuzzy_add(code, anchor, new_code, replace_all,
                                                       before, unit)
        return new_src, count, err, ecode, ({"matched": level} if level else None)
    if count > 1 and not replace_all:
        return None, count, _multi_msg(code, anchor, count), "multiple_matches", None

    body = _prepare(new_code, re.match(r"[ \t]*", anchor).group(0), unit)

    def seg(prev_char: str, next_char: str) -> str:
        out = ""
        if not before:
            if not anchor.endswith("\n") and not body.startswith("\n"):
                out += "\n"
            out += body
            if not body.endswith("\n") and next_char != "\n":
                out += "\n"
        else:
            if not body.endswith("\n") and not anchor.startswith("\n"):
                out += body + "\n"
            else:
                out += body
        return out

    out: List[str] = []
    pos = 0
    inserted = 0
    while True:
        idx = code.find(anchor, pos)
        if idx == -1:
            out.append(code[pos:])
            break
        end = idx + len(anchor)
        if before:
            out.append(code[pos:idx])
            if replace_all or inserted == 0:
                out.append(seg(code[idx - 1:idx], anchor[:1]))
                inserted += 1
            out.append(anchor)
            if not replace_all:
                out.append(code[end:])
                break
            pos = end
            continue
        out.append(code[pos:end])
        if replace_all or inserted == 0:
            out.append(seg(code[end - 1:end], code[end:end + 1]))
            inserted += 1
            if not replace_all:
                out.append(code[end:])
                break
        pos = end
    return "".join(out), count, None, None, None


def _finish(file_path, encoding, newline, action, count, count_key, new_src,
            before=None, dry_run=False, extra=None):
    """落盘并返回精简摘要 + 变更 diff（不含全量源码，省 token）"""
    payload = {"action": action, "file": _short_path(file_path), count_key: count}
    if before is not None:
        payload["lines"] = [len(before.splitlines()), len(new_src.splitlines())]
        text, truncated, _ = _make_diff(before, new_src)
        payload["diff"] = text
        if truncated:
            payload["diff_truncated"] = True
    if extra:
        payload.update(extra)
    if dry_run:
        payload["dry_run"] = True
        return ok(payload)
    werr = write_file(file_path, new_src, encoding, newline)
    if werr:
        return error(werr, code="io_error")
    return ok(payload)


def _create_file(file_path, new_code, dry_run=False):
    """新建文件（file_path 必须不存在）；父目录缺失时自动创建。不回显整文件 diff（内容即入参）。"""
    if not (file_path or "").strip():
        return error("create 需要提供 file_path（不能只给 url）", code="missing_argument")
    if os.path.isdir(file_path):
        return error(f"路径是目录: {file_path}", code="is_directory")
    if os.path.exists(file_path):
        return error(f"文件已存在: {file_path}（改用 edit/add）", code="already_exists")

    text = new_code or ""
    if text and not text.endswith("\n"):
        text += "\n"
    parent = os.path.dirname(os.path.abspath(file_path))
    need_dir = bool(parent) and not os.path.isdir(parent)
    payload = {"action": "create", "file": _short_path(file_path),
               "lines": len(text.splitlines()), "bytes": len(text.encode("utf-8"))}
    if dry_run:
        payload["dry_run"] = True
        return ok(payload)
    if need_dir:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return error(f"创建目录失败 - {e}", code="io_error")
        payload["dir_created"] = parent
    werr = write_file(file_path, text, "utf-8", "\n")
    if werr:
        return error(werr, code="io_error")
    return ok(payload)


def _hint_msg(code: str, old_code: str) -> str:
    """old_code 未命中时附最接近的行窗口（文本形式，兼容两端 error 信封）"""
    cands = _closest_lines(code, old_code)
    if not cands:
        return ""
    rows = "  ".join(f"L{c['lines']}({c['ratio']:.2f})「{(c['text'].splitlines() or [''])[0][:80]}」"
                     for c in cands)
    return f"；最接近 -> {rows}"
