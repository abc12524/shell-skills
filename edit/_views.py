"""输出层：read 动作的渲染（符号体 / 匹配行 / 行范围）与限长截断。

依赖语法层做定位、文本层做行号渲染，本身不修改文件。
"""

import difflib
import re

from typing import List

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

from ._text import _num_width, _numbered

from ._symbols import MAX_CANDIDATES, ParseResult, SymbolRange, _is_plain, _normalize_language, build_skeleton, get_parser


def _resolve_symbols(parsed: ParseResult, symbol, pattern):
    """按符号名（支持裸方法名）与正则筛选，返回 (selected, err_payload)"""
    names = parsed.symbols
    wanted: List[str] = []
    if symbol:
        wanted = [symbol] if isinstance(symbol, str) else [str(x) for x in symbol]
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return None, error(f"正则表达式非法: {e}", code="bad_pattern")

    selected: List[SymbolRange] = []
    seen = set()
    for s in names:
        if rx and rx.search(s.name):
            if s.name not in seen:
                selected.append(s)
                seen.add(s.name)
    for w in wanted:
        exact = [s for s in names if s.name == w]
        if not exact:
            exact = [s for s in names
                     if s.name.replace("::", ".").split(".")[-1] == w or s.name.endswith("." + w)]
        if not exact:
            close = difflib.get_close_matches(w, [s.name for s in names], n=5, cutoff=0.6)
            avail = _symbol_list(names)
            hint = f"，是否想找: {', '.join(close)}" if close else ""
            return None, error(f"未匹配到符号 '{w}'{hint}（可用: {avail}）",
                               code="symbol_not_found")
        if len(exact) > 1 and w not in [s.name for s in names]:
            cands = ", ".join(f"{s.name}(L{s.start})" for s in exact[:10])
            return None, error(f"符号 '{w}' 有 {len(exact)} 个同名成员: {cands}；请用 类名.{w} 指定",
                               code="ambiguous_symbol")
        for s in exact:
            if s.name not in seen:
                selected.append(s)
                seen.add(s.name)
    return selected, None


def _symbol_list(names: List[SymbolRange]) -> str:
    shown = [s.name for s in names[:MAX_CANDIDATES]]
    tail = f" …共 {len(names)} 个" if len(names) > MAX_CANDIDATES else ""
    return ", ".join(shown) + tail


def _emit_symbols(code: str, selected: List[SymbolRange], limit: int, src: dict):
    """输出符号体（带绝对行号），超限截断并给出可直接复用的 next 参数"""
    lines = code.splitlines()
    out: List[str] = []
    shown = 0
    truncated = False
    nxt = None
    for s in selected:
        if shown >= limit:
            truncated = True
            nxt = {"symbol": s.name}
            break
        out.append(f"# {s.name} ({s.kind}) L{s.start}-{s.end}")
        shown += 1
        remain = max(limit - shown, 1)       # 至少留 1 行，否则 next 会指回同一符号
        span = s.end - s.start + 1
        if span > remain:
            out.append(_numbered(lines, s.start, s.start + remain - 1))
            truncated = True
            nxt = {"start_line": s.start + remain, "end_line": s.end}
            break
        out.append(_numbered(lines, s.start, s.end))
        shown += span
    payload = {"action": "read", "mode": "symbol", "result": "\n".join(out)}
    if truncated:
        payload["truncated"] = True
        payload["next"] = nxt
    payload.update(src)
    return ok(payload)


def _emit_matches(code: str, symbol, pattern, limit: int, src: dict, note: str = None):
    """按 symbol（子串）/ pattern（正则）逐行匹配，带绝对行号输出"""
    lines = code.splitlines()
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return error(f"正则表达式非法: {e}", code="bad_pattern")
    wanted: List[str] = []
    if symbol:
        wanted = [symbol] if isinstance(symbol, str) else [str(x) for x in symbol]
    idxs = [i for i, line in enumerate(lines)
            if (rx and rx.search(line)) or any(w in line for w in wanted)]
    if not idxs:
        return error("未匹配到内容", code="not_found")
    shown = idxs[:limit]
    w = _num_width(len(lines))
    body = "\n".join(f"{i + 1:>{w}}: {lines[i]}" for i in shown)
    payload = {"action": "read", "mode": "text", "matches": len(idxs), "result": body}
    if note:
        payload["note"] = note
    if len(idxs) > limit:
        payload["truncated"] = True
        payload["next"] = f"共 {len(idxs)} 处，已显示前 {limit} 处；可加 pattern 收窄"
    payload.update(src)
    return ok(payload)


def _emit_range(code: str, start: int, end: int, limit: int, src: dict, note: str = None):
    """输出行范围（带绝对行号），超限截断并给出 next 续读参数"""
    lines = code.splitlines()
    total = len(lines)
    if start < 1 or end < 1:
        return error(f"行号从 1 开始: {start}-{end}", code="bad_range")
    if start > total:
        return error(f"起始行 {start} 超出总行数 {total}", code="bad_range")
    if start > end:
        return error(f"起始行 {start} 大于结束行 {end}", code="bad_range")
    end = min(end, total)
    truncated = False
    if end - start + 1 > limit:
        end = start + limit - 1
        truncated = True
    payload = {"action": "read", "mode": "lines", "range": f"{start}-{end}", "total": total,
               "result": _numbered(lines, start, end)}
    if note:
        payload["note"] = note
    if truncated:
        payload["truncated"] = True
        payload["next"] = {"start_line": end + 1, "end_line": total}
    payload.update(src)
    return ok(payload)


def _parse_or_fail(code: str, language: str):
    try:
        parsed = get_parser(code, language).parse()
        parsed.language = _normalize_language(language) or parsed.language
        return parsed, None
    except SyntaxError as e:
        return None, error(f"解析失败 - {e}", code="parse_failed")
    except ValueError as e:
        return None, error(str(e), code="unsupported_language")
    except Exception as e:
        return None, error(f"解析失败 - {e}", code="parse_failed")


def _read(code, language, symbol, pattern, start_line, end_line, limit, src, with_calls=False,
          plain_note=None):
    """read：优先 symbol/pattern，其次行范围，最后回退骨架（代码）或全文（纯文本）

    结构解析失败（语法错误的文件很常见）不中断，一律降级为文本检索/文本返回。
    """
    if not code.strip():
        return error("文件内容为空", code="empty_code")

    plain = _is_plain(language)
    total = len(code.splitlines())

    if symbol or pattern:
        if not plain:
            parsed, _ = _parse_or_fail(code, language)
            if parsed is not None:
                selected, serr = _resolve_symbols(parsed, symbol, pattern)
                if selected:
                    return _emit_symbols(code, selected, limit, src)
                if pattern and not symbol:      # 符号名未命中 → 回退按行 grep
                    return _emit_matches(code, None, pattern, limit, src,
                                         note="pattern 未匹配到符号名，已按行检索")
                if serr:
                    return serr
            else:                               # 解析失败 → 降级文本检索
                return _emit_matches(code, symbol, pattern, limit, src,
                                     note="结构解析失败，已按文本检索")
        return _emit_matches(code, symbol, pattern, limit, src, note=plain_note)

    if start_line is not None or end_line is not None:
        try:
            start = int(start_line) if start_line is not None else 1
            end = int(end_line) if end_line is not None else total
        except (TypeError, ValueError):
            return error("start_line / end_line 必须为整数", code="bad_range")
        return _emit_range(code, start, end, limit, src)

    if plain:
        return _emit_range(code, 1, total, limit, src, note=plain_note)
    try:
        text, truncated, next_start = build_skeleton(code, language, limit, with_calls)
    except Exception as e:
        return _emit_range(code, 1, total, limit, src, note=f"结构解析失败（{e}），已按纯文本返回")
    payload = {"action": "read", "mode": "outline", "result": text}
    if truncated:
        payload["truncated"] = True
        payload["next"] = {"action": "outline", "start_line": next_start}
    payload.update(src)
    return ok(payload)
