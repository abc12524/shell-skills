"""入口层：script_editor —— 面向文件的脚本 增 / 删 / 改 / 查（CRUD）+ 结构骨架。

模块分工：
    _text.py     编码/换行探测、读写、行切分与编号渲染、unified diff
    _symbols.py  符号结构解析（python / java / kotlin / c / cpp / csharp / go / rust /
                 php / swift / js / ts / shell）
    _edits.py    行级改写原语、缩进与空行处理、写回与 diff 折叠
    _views.py    read 的渲染与限长截断
    _script_impl.py（本文件）只做参数归一化、定位方式解析与动作分派。

数据来源：file_path（本地文件，可写；原子写回）或 url（http/https，只读）。
"""

from urllib.parse import urlparse
import io
import os
import re
import shutil
import time
import tokenize

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

# 拆出去的名字在这里重新导出，保持 from ._script_impl import xxx 的旧引用仍然可用
from ._text import (  # noqa: F401
    DEFAULT_LIMIT, DEFAULT_MAX_BYTES, DIFF_HUNKS, DIFF_LIMIT, _EXT_LANG, _as_int, _decode_bytes,
    _detect_newline, _join_lines, _looks_binary, _make_diff, _new_lines, _num_width, _numbered,
    _short_path, _split_lines, _symlink_guard, fetch_url, file_identity, file_sha256,
    infer_language, read_file, read_file_head, write_file,
)
from ._symbols import (  # noqa: F401
    MAX_CANDIDATES, MAX_SYMBOLS, PARSERS, BraceParser, CSharpParser, CppParser, GoParser,
    JavaParser, JavaScriptParser, KotlinParser, ParseResult, PhpParser, PythonParser, RustParser,
    ShellParser, SwiftParser, SymbolRange, TypeScriptParser, _LANG_ALIAS, _is_plain,
    _normalize_language, build_skeleton, enclosing_block, get_parser, render_skeleton,
    validate_source,
)
from ._edits import (  # noqa: F401
    _DEF_LIKE_RE, _add_after, _auto_indent, _body_indent, _closest_lines, _create_file,
    _def_gap, _def_like, _finish, _hint_msg, _indent_unit, _insert_at_line, _multi_msg,
    _pad_blank, _prepare, _replace, _replace_lines, _tidy_seam, _validate_mode,
)
from ._views import (  # noqa: F401
    _emit_matches, _emit_range, _emit_symbols, _parse_or_fail, _read, _resolve_symbols,
    _symbol_list,
)


def _norm_text(value):
    if value is None:
        return None
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


MAX_REPLACEMENTS = 200
MAX_PATCH_FILES = 50

_READ_PREFIX_RE = re.compile(r"^\s*(\d+):[ \t]?(.*)$")
_HASHLINE_HEADER_RE = re.compile(r"^\s*\[[^#\r\n]+#[0-9A-Fa-f]{4}\]\s*$")
_READ_RANGE_RE = re.compile(r"^(.*):(\d+)(?:-(\d+))?$")


def _strip_read_prefixes(text):
    """剥离从 read 输出复制来的 `[PATH#TAG]` 头和 `N: ` 行号前缀。

    只有所有非空行都形如 `N: ` 且行号严格递增时才动手，避免误删真实代码里的
    `123: value` 之类内容。返回 (text, stripped)。
    """
    if not text:
        return text, False
    lines = text.split("\n")
    stripped_header = False
    if lines and _HASHLINE_HEADER_RE.match(lines[0] or ""):
        lines = lines[1:]
        stripped_header = True
    matches = []
    last = 0
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        m = _READ_PREFIX_RE.match(line)
        if not m:
            return text, stripped_header
        num = int(m.group(1))
        if num <= last:
            return text, stripped_header
        last = num
        matches.append((idx, m.group(2)))
    if not matches:
        return text, stripped_header
    out = list(lines)
    for idx, content in matches:
        out[idx] = content
    return "\n".join(out), True


def _guard_write_target(file_path):
    """拒绝把 read 的 `path:1-20` 或分号拼接的目标列表当成写路径。"""
    if not file_path:
        return None
    try:
        if os.path.exists(file_path):
            return None
    except OSError:
        pass
    m = _READ_RANGE_RE.match(file_path)
    if m and m.group(1):
        return (f"{file_path} 看起来是 read 的行范围（path:start-end），不是文件路径；"
                f"请改用 file_path={m.group(1)!r}")
    if ";" in file_path:
        parts = [p.strip() for p in file_path.split(";") if p.strip()]
        if len(parts) >= 2 and any(_READ_RANGE_RE.match(p) or re.search(r":\d", p)
                                   for p in parts):
            return f"{file_path} 看起来是多个 read 目标，不是单个文件路径"
    return None


def _block_range(code, language, line):
    """把 enclosing_block 的 (start,end,kind) 包成 SymbolRange，复用符号编辑分支。"""
    found = enclosing_block(code, language, line)
    if not found:
        return None
    start, end, kind = found
    lines = code.splitlines()
    indent = re.match(r"[ \t]*", lines[start - 1]).group(0) if 1 <= start <= len(lines) else ""
    inner_start = start + 1 if end > start else start
    inner_end = end - 1 if end > start else end
    return SymbolRange(
        name=f"block L{start}-{end}", kind="block", start=start, end=end,
        signature=f"{kind} L{start}-{end}", indent=indent,
        body_start=inner_start, body_end=inner_end)


class _DeadlineExceeded(Exception):
    pass


def _deadline(ms):
    if ms is None:
        return None
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return time.monotonic() + value / 1000.0


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() > deadline:
        raise _DeadlineExceeded()


def _norm_replacements(raw, default_all=False, strip=True):
    """批量替换参数归一：[{old_code, new_code, replace_all?, capture?, paste?}]
    -> [(old, new, all, capture, paste)]

    返回 (items, err)。old_code 必填；new_code 缺省为空串（即删除该片段）。
    capture 把 old_code 原文存入命名寄存器并删除；paste 用寄存器内容覆盖 old_code。
    寄存器只在一次 script_editor 调用（patch 的多个文件共享）内有效。
    """
    if not isinstance(raw, (list, tuple)):
        return None, "replacements 需为数组 [{\"old_code\":..., \"new_code\":...}, ...]"
    if not raw:
        return None, "replacements 不能为空数组"
    if len(raw) > MAX_REPLACEMENTS:
        return None, f"replacements 最多 {MAX_REPLACEMENTS} 组，实际 {len(raw)} 组"
    items = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return None, f"第 {i} 组不是对象（应为 {{\"old_code\":..., \"new_code\":...}}）"
        old = _norm_text(item.get("old_code"))
        if not old or not old.strip():
            return None, f"第 {i} 组缺少 old_code"
        new = _norm_text(item.get("new_code")) or ""
        all_ = item.get("replace_all")
        capture = item.get("capture")
        paste = item.get("paste")
        if capture and paste:
            return None, f"第 {i} 组 capture 与 paste 不能同时使用"
        for name, value in (("capture", capture), ("paste", paste)):
            if value is not None and (not isinstance(value, str)
                                      or not re.fullmatch(r"[A-Za-z_]\w*", value)):
                return None, f"第 {i} 组 {name} 需为合法寄存器名（字母/数字/下划线）"
        if capture and new.strip():
            return None, f"第 {i} 组 capture 只做剪切，不能同时给 new_code"
        if strip:
            new, _stripped = _strip_read_prefixes(new)
        items.append((old, new, default_all if all_ is None else bool(all_), capture, paste))
    return items, None


def _snippet(text, width=60):
    """单行摘要，用于回显每组定位片段"""
    first = (text or "").strip().split("\n", 1)[0]
    return first if len(first) <= width else first[:width] + "…"


def _apply_replacements(code, items, unit, deadline=None, registers=None):
    """按顺序应用多组替换（后一组看到前一组的结果）。

    registers 是本次调用共享的命名寄存器（capture 写入、paste 读取）。
    返回 (new_src, rows, err, err_code)；任一组失败即整体放弃（new_src=None）。
    """
    cur = code
    rows = []
    registers = {} if registers is None else registers
    for i, (old, new, all_, capture, paste) in enumerate(items, 1):
        _check_deadline(deadline)
        if paste:
            if paste not in registers:
                return (None, rows,
                        f"第 {i} 组引用了未定义的寄存器 @{paste}（先用 capture 捕获）",
                        "empty_register")
            new = registers[paste]
        if capture:
            registers[capture] = old
        if old == new:
            rows.append({"index": i, "old_code": _snippet(old), "count": 0,
                         "skipped": "identical"})
            if capture:
                rows[-1]["captured"] = capture
            continue
        new_src, count, err, ecode, meta = _replace(cur, old, new, all_, unit)
        if err:
            hint = _hint_msg(cur, old) if ecode == "not_found" else ""
            return None, rows, f"第 {i} 组（{_snippet(old)}）替换失败: {err}{hint}", ecode
        row = {"index": i, "old_code": _snippet(old), "count": count}
        if capture:
            row["captured"] = capture
        if paste:
            row["pasted"] = paste
        if meta:
            row.update(meta)
        rows.append(row)
        cur = new_src
    return cur, rows, None, None


def _norm_patch(file_path, files, edits, rename_to, default_all):
    """patch 参数归一，返回 (items, err)；items 每项含 file_path/edits/rename_to。"""
    if files is not None:
        if file_path or edits is not None or rename_to:
            return None, "files 与顶层 file_path/edits/rename_to 互斥，请只给一种形式"
        if not isinstance(files, (list, tuple)) or not files:
            return None, "files 需为非空数组"
        if len(files) > MAX_PATCH_FILES:
            return None, f"一次最多 {MAX_PATCH_FILES} 个文件，实际 {len(files)} 个"
        items = []
        for i, item in enumerate(files, 1):
            if not isinstance(item, dict):
                return None, f"第 {i} 个文件不是对象"
            path = item.get("path") or item.get("file_path")
            if not path:
                return None, f"第 {i} 个文件缺少 path"
            its, err = _norm_replacements(item.get("edits"), default_all)
            if err:
                return None, f"第 {i} 个文件: {err}"
            items.append({"file_path": path, "edits": its,
                          "rename_to": item.get("rename_to"),
                          "expected_sha256": item.get("expected_sha256")})
        return items, None
    if not file_path:
        return None, "patch 需要 file_path+edits 或 files"
    its, err = _norm_replacements(edits, default_all)
    if err:
        return None, err
    return [{"file_path": file_path, "edits": its, "rename_to": rename_to,
             "expected_sha256": None}], None


def _rename_python(code, old, new):
    lines = code.splitlines(keepends=True)

    def offset(pos):
        row, col = pos
        return sum(len(line) for line in lines[:row - 1]) + col

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, IndentationError):
        return None, 0
    spans = [(offset(t.start), offset(t.end)) for t in tokens
             if t.type == tokenize.NAME and t.string == old]
    for start, end in reversed(spans):
        code = code[:start] + new + code[end:]
    return code, len(spans)


def _rename_in_source(code, language, old, new):
    """按词边界重命名（同文件）；Python 用 tokenize，其余复用 BraceParser 的等长屏蔽文本。"""
    if _normalize_language(language) == "python":
        return _rename_python(code, old, new)
    parser = get_parser(code, language)
    clean = parser.clean
    if len(clean) != len(code):          # 理论上等长；不等长时退回正则直接替换
        return re.sub(r"\b" + re.escape(old) + r"\b", new, code), \
            len(re.findall(r"\b" + re.escape(old) + r"\b", code))
    spans = [m.span() for m in re.finditer(r"\b" + re.escape(old) + r"\b", clean)]
    for start, end in reversed(spans):
        code = code[:start] + new + code[end:]
    return code, len(spans)



def script_editor(action: str = None, path: str = None, file_path: str = None,
                  url: str = None, language: str = None, symbol=None, pattern: str = None,
                  start_line: int = None, end_line: int = None, limit: int = DEFAULT_LIMIT,
                  old_code: str = None, new_code: str = None, replace_all: bool = False,
                  dry_run: bool = False, position: str = None, with_calls: bool = False,
                  diff_context: int = None, replacements=None,
                  expected_sha256: str = None, validate=None, deadline_ms: int = 30000,
                  encoding: str = None, files=None, rename_to: str = None,
                  new_name: str = None, edits=None, block: bool = False,
                  strip_prefixes: bool = True) -> str:
    """脚本编辑 + 结构化读取工具（对齐 OpenViking / DSH 规范信封）。

    数据来源：path（本地文件可写 / http(s) 只读）；file_path、url 为兼容别名。
    动作：read / outline / create / add / edit / delete；patch、rename_symbol 是 edit 的快捷方式。

    edit 的批量替换用 edits（[{"old_code":..,"new_code":..}]，按顺序执行、全成或全败）；
    edit + files 走多文件补丁；edit + symbol + new_name 走符号重命名。
    expected_sha256：read 返回的 file_id 可回传，文件被外部修改时拒绝写入。
    validate：auto（默认，原文件可解析才拒绝）/ on / off；deadline_ms 默认 30000ms。
    """
    act = (action or "").strip().lower()
    content_given = new_code is not None       # 显式空串也算给了内容（建空文件）
    old_code = _norm_text(old_code)
    new_code = _norm_text(new_code) or ""
    stripped_prefixes = False
    if strip_prefixes and new_code:
        new_code, stripped_prefixes = _strip_read_prefixes(new_code)
    pos = (position or "").strip().lower()
    try:
        limit = int(limit) if limit else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    if limit < 1:
        limit = DEFAULT_LIMIT
    try:
        diff_context = int(diff_context) if diff_context is not None else None
    except (TypeError, ValueError):
        return error("diff_context 必须为非负整数", code="invalid_argument")
    if diff_context is not None and diff_context < 0:
        return error("diff_context 必须为非负整数", code="invalid_argument")
    validate_mode = _validate_mode(validate)
    deadline = _deadline(deadline_ms)

    # 统一数据源：path 自动区分本地路径与 http(s) URL；file_path/url 保留为别名
    if path and (file_path or url):
        return error("path 与 file_path/url 互斥，请只给一种来源", code="conflicting_target")
    if path:
        if re.match(r"^https?://", path):
            url = path
        else:
            file_path = path

    if act not in ("read", "outline", "add", "edit", "delete", "create", "patch",
                   "rename_symbol"):
        return error(f"未知操作 '{action}'（应为 read/outline/add/edit/delete/create/patch/"
                     f"rename_symbol）", code="unknown_action")

    # 动作合并：edit + files -> patch；edit + new_name -> rename_symbol
    if act == "edit" and files is not None:
        if new_name:
            return error("files 与 new_name 不能同时使用", code="conflicting_target")
        act = "patch"
    elif act == "edit" and new_name:
        act = "rename_symbol"

    # 批量编辑参数统一为 edits；replacements 保留为别名
    if act == "patch" and edits is None:
        edits = replacements
    if act == "edit" and replacements is None and edits is not None:
        replacements = edits

    rpl = None
    if act == "edit" and replacements is not None:
        rpl, rerr = _norm_replacements(replacements, replace_all)
        if rerr:
            return error(rerr, code="invalid_argument")

    src = {}
    if file_path:
        src["file"] = _short_path(file_path)
    elif url:
        src["url"] = url
    elif act != "patch":
        return error("需要提供 path（本地路径或 URL）", code="missing_source")

    if rpl is not None:
        if symbol or start_line is not None or old_code or block:
            return error("edits/replacements 与 symbol/start_line/old_code/block 互斥，"
                         "请只给一种定位方式", code="conflicting_target")
        if new_code:
            return error("使用 edits/replacements 时不要再给顶层 new_code（每组自带）",
                         code="conflicting_target")

    if block:
        if symbol or old_code:
            return error("block 与 symbol/old_code 互斥，请只给一种定位方式",
                         code="conflicting_target")
        if start_line is None:
            return error("block 需要提供 start_line（块内任意行）", code="missing_argument")

    # 写动作（含 create）一律拒绝软链接：os.replace 会用普通文件顶掉链接节点，
    # 把链接关系毁掉；只读动作不受影响
    write_actions = ("add", "edit", "delete", "create", "patch", "rename_symbol")
    if act in write_actions and file_path:
        link_err = _symlink_guard(file_path)
        if link_err:
            return error(link_err, code="is_symlink")
        target_err = _guard_write_target(file_path)
        if target_err:
            return error(target_err, code="looks_like_read_target")
    if act == "patch":
        return _run_patch(files, file_path, edits, rename_to, replace_all,
                          dry_run=dry_run, diff_context=diff_context,
                          validate_mode=validate_mode, deadline=deadline,
                          encoding=encoding)

    if act == "rename_symbol":
        if not file_path:
            return error("rename_symbol 需要本地 file_path", code="missing_source")
        if not new_name or not re.fullmatch(r"[A-Za-z_]\w*", new_name):
            return error("new_name 需为合法标识符", code="invalid_argument")
        return _run_rename_symbol(file_path, symbol, new_name, language, dry_run=dry_run,
                                  diff_context=diff_context, validate_mode=validate_mode,
                                  deadline=deadline, encoding=encoding,
                                  expected_sha256=expected_sha256)

    if act == "create":
        if not content_given:
            return error("create 需要 new_code（文件内容；也可用 content 别名）",
                         code="missing_argument")
        if not file_path:
            return error("create 需要提供 file_path（不能只给 url）", code="missing_argument")
        lang = language or infer_language(file_path)
        # 新建文件无上下文缩进，`@n:` 以行首为基准、按 4 空格换算
        prepared = _prepare(new_code, "", "    ")
        if validate_mode != "off" and lang and not _is_plain(lang):
            ok_src, verr = validate_source(prepared, lang)
            if not ok_src:
                return error(f"新建内容语法校验失败 - {verr}", code="parse_failed")
        result = _create_file(file_path, prepared, dry_run)
        if stripped_prefixes and result.get("status") == "ok":
            result["result"]["stripped_prefixes"] = True
        return result

    file_encoding, newline = "utf-8", "\n"
    writable = False
    cur_sha = None
    big_file = False
    if file_path:
        _check_deadline(deadline)
        max_bytes = DEFAULT_MAX_BYTES if act == "read" else None
        code, file_encoding, newline, io_err = read_file(file_path, max_bytes=max_bytes,
                                                         encoding=encoding)
        if io_err and act == "read" and io_err.startswith("文件过大"):
            code, file_encoding, io_err = read_file_head(file_path, DEFAULT_MAX_BYTES, encoding)
            big_file = True
        if io_err:
            code_err = ("is_directory" if "目录" in io_err else
                        "binary_file" if "二进制" in io_err else "io_error")
            return error(io_err, code=code_err)
        writable = True
        cur_sha = file_sha256(file_path)
        if cur_sha:
            src["file_id"] = cur_sha
        if big_file:
            src["big_file"] = True
        if not language:
            language = infer_language(file_path)
    else:
        code, file_encoding, io_err = fetch_url(url, encoding=encoding)
        if io_err:
            return error(io_err, code="io_error")
        if not language:
            language = infer_language(urlparse(url).path)
    if code is None:
        code = ""

    # 读前校验：read 拿到的 file_id 回传后，可确保中间没有被外部改过
    if expected_sha256 and cur_sha and str(expected_sha256) != cur_sha:
        return error("文件在读取后已被修改（sha256 不匹配），请重新 read 后再编辑",
                     code="stale_file")

    # 显式传了不支持结构解析的语言时给一句提示（.md/.json 等不传 language，不受影响）
    lang_note = (f"language={language} 无结构解析，已按纯文本处理"
                 if language and _is_plain(language) else None)

    if act == "read":
        if block:
            block_line = _as_int(start_line)
            block_sym = _block_range(code, language, block_line) if block_line else None
            if not block_sym:
                return error("未找到包含该行的语法块，请用 outline 或 read 确认行号",
                             code="block_not_found")
            return _emit_range(code, block_sym.start, block_sym.end, limit, src,
                               note=block_sym.signature)
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
    line_shifted = None
    unit = _indent_unit(lines)
    top_gap = 2 if _normalize_language(language) == "python" else 1

    def finish(op, count, count_key, new_src, before=None, extra=None):
        extra = dict(extra or {})
        if line_shifted:
            extra["line_shifted"] = line_shifted
        if stripped_prefixes:
            extra["stripped_prefixes"] = True
        return _finish(file_path, file_encoding, newline, op, count, count_key, new_src,
                       before=before, dry_run=dry_run, diff_context=diff_context,
                       extra=extra, validate=validate_mode, language=language)

    if rpl is not None:
        # 批量替换：顺序应用、一次落盘；任一组失败则整体放弃（文件保持原样）
        try:
            new_src, rows, berr, becode = _apply_replacements(code, rpl, unit, deadline)
        except _DeadlineExceeded:
            return error("操作超时（deadline_ms）", code="timeout")
        if berr:
            return error(berr, code=becode or "replace_failed")
        if new_src == code:
            return error("replacements 未产生任何变化（各组 new_code 与 old_code 相同）",
                         code="no_change")
        total = sum(row["count"] for row in rows)
        return finish("edit", total, "replaced", new_src,
                      before=code, extra={"replacements": rows})

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
                # 行号已漂移：改用 old_code 内容重新定位，而不是直接拒绝
                line_shifted = f"{s}-{e}"
                line_mode = False
        else:
            return error(f"行范围非法: {s}-{e}（共 {len(lines)} 行）", code="bad_range")

    if use_symbol:
        if _is_plain(language):
            return error("纯文本文件无符号结构，请用 start_line 或 old_code 定位",
                         code="unsupported_language")
        parsed, perr = _parse_or_fail(code, language)
        if perr:
            return error(perr, code="parse_failed")
        selected, serr = _resolve_symbols(parsed, symbol, None)
        if serr:
            return error(serr, code="symbol_not_found")
        if len(selected) != 1:
            cands = ", ".join(f"{s.name}(L{s.start}-L{s.end})" for s in selected[:10])
            return error(f"symbol 需唯一命中，实际 {len(selected)} 个: {cands}",
                         code="ambiguous_symbol")
        sym = selected[0]

    if block and sym is None:
        block_sym = _block_range(code, language, _as_int(start_line))
        if not block_sym:
            return error("未找到包含该行的语法块，请用 outline 或 read 确认行号",
                         code="block_not_found")
        sym = block_sym

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
            new_src = _tidy_seam(new_src, s, top_gap)
            return finish("delete", 1, "deleted", new_src,
                          before=code, extra={"symbol": sym.name, "range": f"{s}-{e}"})
        if line_mode:
            s = _as_int(start_line)
            e = _as_int(end_line) or s
            new_src, err = _replace_lines(code, s, e, "")
            if err:
                return error(err, code="bad_range")
            new_src = _tidy_seam(new_src, s, top_gap)
            return finish("delete", 1, "deleted", new_src,
                          before=code, extra={"range": f"{s}-{e}"})
        if not old_code:
            return error("delete 需要提供 symbol、start_line 或 old_code", code="missing_argument")
        new_src, count, err, ecode, meta = _replace(code, old_code, "", replace_all, unit)
        if err:
            return error(err + (_hint_msg(code, old_code) if ecode == "not_found" else ""),
                         code=ecode)
        return finish("delete", count, "deleted", new_src, before=code, extra=meta)

    if act == "edit":
        if sym is not None:
            if pos not in ("", "before", "after"):
                if pos in ("start", "end"):
                    return error(
                        f"edit+symbol 不支持 position='{position}'：省略 position 表示整段替换符号，"
                        "前后插入用 before/after；往符号体内插内容请用 add",
                        code="invalid_argument")
                return error(
                    f"position 非法 '{position}'（edit+symbol 仅支持 before/after）",
                    code="invalid_argument")
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
            return finish("edit", 1, "replaced", new_src,
                          before=code, extra={"symbol": sym.name, "range": f"{s}-{e}"})
        if line_mode:
            s, e = _as_int(start_line), _as_int(end_line)
            indent = re.match(r"[ \t]*", lines[s - 1]).group(0) if 1 <= (s or 0) <= len(lines) else ""
            new_src, err = _replace_lines(code, s, e, _prepare(new_code, indent, unit))
            if err:
                return error(err, code="bad_range")
            return finish("edit", 1, "replaced", new_src,
                          before=code, extra={"range": f"{s}-{e or s}"})
        if not old_code:
            return error("edit 需要提供 symbol、start_line 或 old_code", code="missing_argument")
        if new_code == old_code:
            return error("new_code 与 old_code 相同，无需修改", code="no_change")
        new_src, count, err, ecode, meta = _replace(code, old_code, new_code, replace_all, unit)
        if err:
            return error(err + (_hint_msg(code, old_code) if ecode == "not_found" else ""),
                         code=ecode)
        return finish("edit", count, "replaced", new_src, before=code, extra=meta)

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
            body = _pad_blank(lines, at, body, next_is_def=next_is_def, top_gap=top_gap)
        new_src, err = _insert_at_line(code, at, body)
        if err:
            return error(err, code="bad_range")
        return finish("add", 1, "inserted", new_src,
                      before=code,
                      extra={"symbol": sym.name, "at_line": at, "position": where})
    if line_mode:
        at = _as_int(start_line)
        if at is None or at < 0 or at > len(lines):
            return error(f"插入位置需在 0-{len(lines)}: {start_line}", code="bad_range")
        anchor = lines[at - 1] if 1 <= at <= len(lines) else ""
        indent = re.match(r"[ \t]*", anchor).group(0) if anchor.strip() else ""
        body = _pad_blank(lines, at, _prepare(new_code, indent, unit),
                          next_is_def=at < len(lines) and _def_like(lines[at]), top_gap=top_gap)
        new_src, err = _insert_at_line(code, at, body)
        if err:
            return error(err, code="bad_range")
        return finish("add", 1, "inserted", new_src, before=code, extra={"at_line": at})
    if not old_code:
        body = _prepare(new_code, "", unit)
        # 追加到文件末尾的新定义：与上一行之间按顶层/成员补足空行（顶层 2 行、成员 1 行）
        if _def_like(body) and lines and not body.startswith("\n"):
            trailing = len(code) - len(code.rstrip("\n"))
            need = _def_gap(body, top_gap) + 1 - trailing
            if need > 0:
                body = "\n" * need + body
        sep = "" if not code or code.endswith("\n") or body.startswith("\n") else "\n"
        new_src = code + sep + body
        if not new_src.endswith("\n"):
            new_src += "\n"
        return finish("add", 1, "inserted", new_src,
                      before=code, extra={"at_line": len(lines) + 1})
    # old_code 锚点：缩进以锚点行自身为基准（`@n:` 同理）
    new_src, count, err, ecode, meta = _add_after(
        code, old_code, new_code, replace_all, before=(pos == "before"), unit=unit,
        top_gap=top_gap)
    if err:
        return error(err + (_hint_msg(code, old_code) if ecode == "not_found" else ""),
                     code=ecode)
    return finish("add", count, "inserted", new_src, before=code, extra=meta)


def _run_rename_symbol(file_path, symbol, new_name, language, dry_run=False,
                       diff_context=None, validate_mode="auto", deadline=None,
                       encoding=None, expected_sha256=None):
    act = "rename_symbol"
    link_err = _symlink_guard(file_path)
    if link_err:
        return error(link_err, code="is_symlink")
    if not symbol:
        return error("rename_symbol 需要提供 symbol", code="missing_argument")
    code, file_encoding, newline, io_err = read_file(file_path, encoding=encoding)
    if io_err:
        return error(io_err, code="io_error")
    cur_sha = file_sha256(file_path)
    if expected_sha256 and cur_sha and str(expected_sha256) != cur_sha:
        return error("文件在读取后已被修改（sha256 不匹配），请重新 read 后再编辑",
                     code="stale_file")
    lang = language or infer_language(file_path)
    if _is_plain(lang):
        return error("纯文本文件无符号结构，无法按符号重命名", code="unsupported_language")
    parsed, perr = _parse_or_fail(code, lang)
    if perr:
        return error(perr, code="parse_failed")
    selected, serr = _resolve_symbols(parsed, symbol, None)
    if serr:
        return error(serr, code="symbol_not_found")
    if len(selected) != 1:
        cands = ", ".join(f"{s.name}(L{s.start}-L{s.end})" for s in selected[:10])
        return error(f"symbol 需唯一命中，实际 {len(selected)} 个: {cands}", code="ambiguous_symbol")
    sym = selected[0]
    token = sym.name.split(".")[-1]
    if token == new_name:
        return error("new_name 与原符号名相同，无需修改", code="no_change")
    _check_deadline(deadline)
    try:
        new_src, count = _rename_in_source(code, lang, token, new_name)
    except Exception as e:
        return error(f"重命名失败 - {e}", code="rename_failed")
    if new_src is None:
        return error("重命名失败：无法解析源码 token", code="parse_failed")
    if validate_mode != "off":
        ok_before, _ = validate_source(code, lang)
        ok_after, verr = validate_source(new_src, lang)
        if not ok_after and (validate_mode == "on" or ok_before):
            return error(f"重命名后语法校验失败，已放弃写入 - {verr}", code="parse_failed")
    return _finish(file_path, file_encoding, newline, act, count, "renamed", new_src,
                   before=code, dry_run=dry_run, diff_context=diff_context,
                   extra={"symbol": sym.name, "new_name": new_name},
                   validate=validate_mode, language=lang)


def _run_patch(files, file_path, edits, rename_to, default_all, dry_run=False,
               diff_context=None, validate_mode="auto", deadline=None, encoding=None):
    items, err = _norm_patch(file_path, files, edits, rename_to, default_all)
    if err:
        return error(err, code="invalid_argument")
    staged = []
    registers = {}
    try:
        for item in items:
            _check_deadline(deadline)
            path = item["file_path"]
            link_err = _symlink_guard(path)
            if link_err:
                return error(link_err, code="is_symlink")
            target_err = _guard_write_target(path)
            if target_err:
                return error(f"{path}: {target_err}", code="looks_like_read_target")
            code, file_encoding, newline, io_err = read_file(path, encoding=encoding)
            if io_err:
                return error(f"{path}: {io_err}", code="io_error")
            cur_sha = file_sha256(path)
            if item["expected_sha256"] and cur_sha and str(item["expected_sha256"]) != cur_sha:
                return error(f"{path}: 文件在读取后已被修改（sha256 不匹配）", code="stale_file")
            lang = infer_language(path)
            unit = _indent_unit(code.splitlines())
            new_src, rows, berr, becode = _apply_replacements(code, item["edits"], unit,
                                                              deadline, registers=registers)
            if berr:
                return error(f"{path}: {berr}", code=becode or "replace_failed")
            if new_src == code:
                return error(f"{path}: edits 未产生任何变化", code="no_change")
            target = item["rename_to"]
            if target:
                if os.path.exists(target):
                    return error(f"重命名目标已存在: {target}", code="already_exists")
            if validate_mode != "off" and lang and not _is_plain(lang):
                ok_before, _ = validate_source(code, lang)
                ok_after, verr = validate_source(new_src, lang)
                if not ok_after and (validate_mode == "on" or ok_before):
                    return error(f"{path}: 编辑后语法校验失败 - {verr}", code="parse_failed")
            diff_text, truncated, _ = _make_diff(code, new_src, diff_context)
            staged.append({"path": path, "target": target, "before": code, "after": new_src,
                           "encoding": file_encoding, "newline": newline,
                           "replaced": sum(r["count"] for r in rows), "rows": rows,
                           "diff": diff_text, "truncated": truncated})
    except _DeadlineExceeded:
        return error("操作超时（deadline_ms）", code="timeout")

    report = []
    for st in staged:
        row = {"file": _short_path(st["path"]), "replaced": st["replaced"], "diff": st["diff"],
               "edit_rows": st["rows"]}
        if st["truncated"]:
            row["diff_truncated"] = True
        if st["target"]:
            row["renamed_to"] = _short_path(st["target"])
        report.append(row)

    if dry_run:
        return ok({"action": "patch", "files": report, "files_changed": len(report),
                   "dry_run": True})

    written = []
    moved = []
    try:
        for st in staged:
            werr = write_file(st["path"], st["after"], st["encoding"], st["newline"])
            if werr:
                raise OSError(f"{st['path']}: {werr}")
            written.append(st)
            if st["target"]:
                parent = os.path.dirname(os.path.abspath(st["target"]))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.move(st["path"], st["target"])
                moved.append(st)
    except (OSError, shutil.Error) as e:
        # 回滚：已移动的文件先移回，再用 before 内容重写已写文件
        for st in reversed(moved):
            try:
                if os.path.exists(st["target"]) and not os.path.exists(st["path"]):
                    shutil.move(st["target"], st["path"])
            except (OSError, shutil.Error):
                pass
        for st in reversed(written):
            write_file(st["path"], st["before"], st["encoding"], st["newline"])
        return error(f"patch 落盘失败，已回滚 - {e}", code="io_error")

    return ok({"action": "patch", "files": report, "files_changed": len(report)})