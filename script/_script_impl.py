"""入口层：script_editor —— 面向文件的脚本 增 / 删 / 改 / 查（CRUD）+ 结构骨架。

模块分工：
    _text.py     编码/换行探测、读写、行切分与编号渲染、unified diff
    _symbols.py  符号结构解析（python / java / kotlin / c / cpp / csharp / js / ts / shell）
    _edits.py    行级改写原语、缩进与空行处理、写回与 diff 折叠
    _views.py    read 的渲染与限长截断
    _script_impl.py（本文件）只做参数归一化、定位方式解析与动作分派。

数据来源：file_path（本地文件，可写；原子写回）或 url（http/https，只读）。
语言：python / java / kotlin / c / cpp / csharp / javascript / typescript / shell
"""

from urllib.parse import urlparse
import re

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

# 拆出去的名字在这里重新导出，保持 from ._script_impl import xxx 的旧引用仍然可用
from ._text import (  # noqa: F401
    DEFAULT_LIMIT, DIFF_HUNKS, DIFF_LIMIT, _EXT_LANG, _as_int, _decode_bytes, _detect_newline,
    _join_lines, _looks_binary, _make_diff, _new_lines, _num_width, _numbered, _short_path,
    _split_lines, _symlink_guard, fetch_url, infer_language, read_file, write_file,
)
from ._symbols import (  # noqa: F401
    MAX_CANDIDATES, MAX_SYMBOLS, PARSERS, BraceParser, CSharpParser, CppParser, JavaParser,
    JavaScriptParser, KotlinParser, ParseResult, PythonParser, ShellParser, SymbolRange,
    TypeScriptParser, _LANG_ALIAS, _is_plain, _normalize_language, build_skeleton, get_parser,
    render_skeleton,
)
from ._edits import (  # noqa: F401
    _DEF_LIKE_RE, _add_after, _auto_indent, _body_indent, _closest_lines, _create_file,
    _def_like, _finish, _hint_msg, _indent_unit, _insert_at_line, _multi_msg, _pad_blank,
    _prepare, _replace, _replace_lines,
)
from ._views import (  # noqa: F401
    _emit_matches, _emit_range, _emit_symbols, _parse_or_fail, _read, _resolve_symbols,
    _symbol_list,
)


def _norm_text(value):
    if value is None:
        return None
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def script_editor(action: str = None, file_path: str = None, url: str = None,
                  language: str = None, symbol=None, pattern: str = None,
                  start_line: int = None, end_line: int = None, limit: int = DEFAULT_LIMIT,
                  old_code: str = None, new_code: str = None, replace_all: bool = False,
                  dry_run: bool = False, position: str = None, with_calls: bool = False) -> str:
    """脚本编辑 + 结构化读取工具（对齐 OpenViking / DSH 规范信封）。

    数据来源（二选一）：file_path（本地文件，可写）或 url（http/https，只读）。

    动作：
    - read    : symbol（单个/数组，支持裸方法名）/ pattern（正则，先符号名后按行）/
                start_line~end_line 行范围；都不给则返回结构骨架
    - outline : 结构骨架（含每个符号起止行号；with_calls=true 才附调用关系）
    - create  : 新建文件（file_path 必须不存在；父目录缺失时自动创建）
    - add     : 插入 new_code，定位优先级 symbol > start_line > old_code > 文件末尾
    - edit    : 替换，定位优先级 symbol > start_line > old_code
    - delete  : 删除，定位优先级 symbol > start_line > old_code
    position（配合 symbol）：start | end | before | after（add 默认 end，其余默认整体）
    写入动作默认回显 unified diff；dry_run=true 只预览不落盘。
    """
    act = (action or "").strip().lower()
    old_code = _norm_text(old_code)
    new_code = _norm_text(new_code) or ""
    pos = (position or "").strip().lower()
    try:
        limit = int(limit) if limit else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    if limit < 1:
        limit = DEFAULT_LIMIT

    src = {}
    if file_path:
        src["file"] = _short_path(file_path)
    elif url:
        src["url"] = url
    else:
        return error("需要提供 file_path 或 url", code="missing_source")

    if act not in ("read", "outline", "add", "edit", "delete", "create"):
        return error(f"未知操作 '{action}'（应为 read/outline/add/edit/delete/create）",
                     code="unknown_action")

    # 写动作（含 create）一律拒绝软链接：os.replace 会用普通文件顶掉链接节点，
    # 把链接关系毁掉；只读动作不受影响
    if act in ("add", "edit", "delete", "create"):
        link_err = _symlink_guard(file_path)
        if link_err:
            return error(link_err, code="is_symlink")

    if act == "create":
        # 新建文件无上下文缩进，`@n:` 以行首为基准、按 4 空格换算
        return _create_file(file_path, _prepare(new_code, "", "    "), dry_run)

    encoding, newline = "utf-8", "\n"
    writable = False
    if file_path:
        code, encoding, newline, io_err = read_file(file_path)
        if io_err:
            code_err = ("is_directory" if "目录" in io_err else
                        "binary_file" if "二进制" in io_err else "io_error")
            return error(io_err, code=code_err)
        writable = True
        if not language:
            language = infer_language(file_path)
    else:
        code, encoding, io_err = fetch_url(url)
        if io_err:
            return error(io_err, code="io_error")
        if not language:
            language = infer_language(urlparse(url).path)
    if code is None:
        code = ""

    # 显式传了不支持结构解析的语言时给一句提示（.md/.json 等不传 language，不受影响）
    lang_note = (f"language={language} 无结构解析，已按纯文本处理"
                 if language and _is_plain(language) else None)

    if act == "read":
        return _read(code, language, symbol, pattern, start_line, end_line, limit, src,
                     with_calls, lang_note)

    if act == "outline":
        if not code.strip():
            return error("文件内容为空", code="empty_code")
        if _is_plain(language):
            text, truncated, _ = render_skeleton(
                ParseResult("text", len(code.splitlines()), [], [], [], {}), limit)
            payload = {"action": "outline", "result": text}
            if lang_note:
                payload["note"] = lang_note
            if truncated:
                payload["truncated"] = True
            payload.update(src)
            return ok(payload)
        try:
            text, truncated, next_start = build_skeleton(code, language, limit, with_calls,
                                                        _as_int(start_line))
        except SyntaxError as e:
            return error(f"解析失败 - {e}", code="parse_failed")
        except ValueError as e:
            return error(str(e), code="unsupported_language")
        except Exception as e:
            return error(f"解析失败 - {e}", code="parse_failed")
        payload = {"action": "outline", "language": _normalize_language(language), "result": text}
        if truncated:
            payload["truncated"] = True
            payload["next"] = {"action": "outline", "start_line": next_start}
        payload.update(src)
        return ok(payload)

    # add / edit / delete 需要可写来源
    if not writable:
        return error("url 为只读，不能修改", code="read_only")

    lines = code.splitlines()
    unit = _indent_unit(lines)
    sym = None
    use_symbol = bool(symbol)
    line_mode = start_line is not None

    if use_symbol and (line_mode or old_code):
        return error("symbol 与 start_line/old_code 互斥，请只给一种定位方式",
                     code="conflicting_target")
    if line_mode and old_code and act in ("edit", "delete"):
        s, e = _as_int(start_line), _as_int(end_line)
        if s and e and s >= 1 and e <= len(lines) and s <= e:
            actual = "\n".join(lines[s - 1:e])
            if actual.rstrip("\n") != old_code.rstrip("\n") and actual.strip() != old_code.strip():
                return error(
                    f"start_line~end_line ({s}-{e}) 的内容与 old_code 不一致，已中止以免改错行；"
                    f"实际内容为:\n{_numbered(lines, s, min(e, s + 20))}",
                    code="range_mismatch")
        else:
            return error(f"行范围非法: {s}-{e}（共 {len(lines)} 行）", code="bad_range")

    if use_symbol:
        if _is_plain(language):
            return error("纯文本文件无符号结构，请用 start_line 或 old_code 定位",
                         code="unsupported_language")
        parsed, perr = _parse_or_fail(code, language)
        if perr:
            return perr
        selected, serr = _resolve_symbols(parsed, symbol, None)
        if serr:
            return serr
        if len(selected) != 1:
            cands = ", ".join(f"{s.name}(L{s.start}-{s.end})" for s in selected[:10])
            return error(f"symbol 需唯一命中，实际 {len(selected)} 个: {cands}",
                         code="ambiguous_symbol")
        sym = selected[0]

    if act == "delete":
        if sym is not None:
            s, e = sym.start, sym.end
            # 连同紧随的空行一起删（最多 2 行）；以符号末尾为基准，否则长符号会把自身跨度
            # 算进容差，导致一个空行都不删
            while e + 1 <= len(lines) and e - sym.end < 2 and not lines[e].strip():
                e += 1
            new_src, err = _replace_lines(code, s, e, "")
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "delete", 1, "deleted", new_src,
                           before=code, dry_run=dry_run,
                           extra={"symbol": sym.name, "range": f"{s}-{e}"})
        if line_mode:
            new_src, err = _replace_lines(code, _as_int(start_line), _as_int(end_line), "")
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "delete", 1, "deleted", new_src,
                           before=code, dry_run=dry_run,
                           extra={"range": f"{_as_int(start_line)}-{_as_int(end_line) or _as_int(start_line)}"})
        if not old_code:
            return error("delete 需要提供 symbol、start_line 或 old_code", code="missing_argument")
        new_src, count, err, ecode, meta = _replace(code, old_code, "", replace_all, unit)
        if err:
            return error(err + (_hint_msg(code, old_code) if ecode == "not_found" else ""),
                         code=ecode)
        return _finish(file_path, encoding, newline, "delete", count, "deleted", new_src,
                       before=code, dry_run=dry_run, extra=meta)

    if act == "edit":
        if sym is not None:
            s, e = sym.start, sym.end
            body = _prepare(new_code, sym.indent, unit)
            if pos == "before":
                new_src, err = _insert_at_line(code, s - 1, body)
            elif pos == "after":
                new_src, err = _insert_at_line(code, e, body)
            else:
                new_src, err = _replace_lines(code, s, e, body)
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "edit", 1, "replaced", new_src,
                           before=code, dry_run=dry_run,
                           extra={"symbol": sym.name, "range": f"{s}-{e}"})
        if line_mode:
            s, e = _as_int(start_line), _as_int(end_line)
            indent = re.match(r"[ \t]*", lines[s - 1]).group(0) if 1 <= (s or 0) <= len(lines) else ""
            new_src, err = _replace_lines(code, s, e, _prepare(new_code, indent, unit))
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "edit", 1, "replaced", new_src,
                           before=code, dry_run=dry_run, extra={"range": f"{s}-{e or s}"})
        if not old_code:
            return error("edit 需要提供 symbol、start_line 或 old_code", code="missing_argument")
        if new_code == old_code:
            return error("new_code 与 old_code 相同，无需修改", code="no_change")
        new_src, count, err, ecode, meta = _replace(code, old_code, new_code, replace_all, unit)
        if err:
            return error(err + (_hint_msg(code, old_code) if ecode == "not_found" else ""),
                         code=ecode)
        return _finish(file_path, encoding, newline, "edit", count, "replaced", new_src,
                       before=code, dry_run=dry_run, extra=meta)

    # ---------------- add ----------------
    if not new_code:
        return error("add 需要提供 new_code", code="missing_argument")
    if sym is not None:
        where = pos or "end"
        if where == "end":
            at, indent = sym.body_end, _body_indent(lines, sym)
        elif where == "start":
            at, indent = sym.body_start - 1, _body_indent(lines, sym)
        elif where == "after":
            at, indent = sym.end, sym.indent
        elif where == "before":
            at, indent = sym.start - 1, sym.indent
        else:
            return error(f"position 非法 '{position}'（应为 start/end/before/after）",
                         code="invalid_argument")
        body = _prepare(new_code, indent, unit)
        if where != "start":             # 插进体的首行属于语句流，不额外补空行
            next_is_def = where == "before" or (at < len(lines) and _def_like(lines[at]))
            body = _pad_blank(lines, at, body, next_is_def=next_is_def)
        new_src, err = _insert_at_line(code, at, body)
        if err:
            return error(err, code="bad_range")
        return _finish(file_path, encoding, newline, "add", 1, "inserted", new_src,
                       before=code, dry_run=dry_run,
                       extra={"symbol": sym.name, "at_line": at, "position": where})
    if line_mode:
        at = _as_int(start_line)
        if at is None or at < 0 or at > len(lines):
            return error(f"插入位置需在 0-{len(lines)}: {start_line}", code="bad_range")
        anchor = lines[at - 1] if 1 <= at <= len(lines) else ""
        indent = re.match(r"[ \t]*", anchor).group(0) if anchor.strip() else ""
        body = _pad_blank(lines, at, _prepare(new_code, indent, unit),
                          next_is_def=at < len(lines) and _def_like(lines[at]))
        new_src, err = _insert_at_line(code, at, body)
        if err:
            return error(err, code="bad_range")
        return _finish(file_path, encoding, newline, "add", 1, "inserted", new_src,
                       before=code, dry_run=dry_run, extra={"at_line": at})
    if not old_code:
        body = _prepare(new_code, "", unit)
        # 追加到文件末尾的新定义：与上一行之间留一个空行
        if (_def_like(body) and lines and lines[-1].strip()
                and not code.endswith("\n\n") and not body.startswith("\n")):
            body = "\n" + body
        sep = "" if not code or code.endswith("\n") or body.startswith("\n") else "\n"
        new_src = code + sep + body
        if not new_src.endswith("\n"):
            new_src += "\n"
        return _finish(file_path, encoding, newline, "add", 1, "inserted", new_src,
                       before=code, dry_run=dry_run, extra={"at_line": len(lines) + 1})
    # old_code 锚点：缩进以锚点行自身为基准（`@n:` 同理）
    new_src, count, err, ecode, meta = _add_after(code, old_code, new_code, replace_all,
                                                  before=(pos == "before"), unit=unit)
    if err:
        return error(err + (_hint_msg(code, old_code) if ecode == "not_found" else ""),
                     code=ecode)
    return _finish(file_path, encoding, newline, "add", count, "inserted", new_src,
                   before=code, dry_run=dry_run, extra=meta)
