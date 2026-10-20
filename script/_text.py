"""文本层：编码/换行探测、文件与 URL 读写、行切分与编号渲染、unified diff。

不含语言语法知识与定位决策，只提供「把文件读成行、把行写回文件」这一层的原语。
"""

from urllib.request import urlopen
import difflib
import os

from typing import List, Optional

_EXT_LANG = {
    ".py": "python", ".pyw": "python",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
}


def infer_language(file_path: str) -> Optional[str]:
    """按扩展名推断语言（用于 file_path 模式自动补齐 language）"""
    return _EXT_LANG.get(os.path.splitext(file_path or "")[1].lower())


def _looks_binary(raw: bytes) -> bool:
    chunk = raw[:8192]
    if b"\x00" in chunk:
        return True
    text_chars = bytearray({7, 8, 9, 10, 11, 12, 13, 27, 0x81, 0x8d, 0x8f, 0x90, 0x9d})
    odd = sum(1 for b in chunk if b < 32 and b not in text_chars)
    return bool(chunk) and odd / float(len(chunk)) > 0.30


def _decode_bytes(raw: bytes):
    """自动探测编码并解码，返回 (text, encoding)。latin-1 兜底不会失败。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1"), "latin-1"


def _detect_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def read_file(file_path: str):
    """读取本地文件，返回 (text, encoding, newline, err)。

    text 统一为 '\\n' 换行，newline 记录原文件换行风格（写入时还原）。
    """
    if os.path.isdir(file_path):
        return None, None, None, f"路径是目录: {file_path}"
    try:
        with open(file_path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, None, None, f"文件不存在: {file_path}"
    except OSError as e:
        return None, None, None, f"读取失败 - {e}"
    if _looks_binary(raw):
        return None, None, None, f"二进制文件不支持按文本处理: {file_path}（{len(raw)} bytes）"
    text, enc = _decode_bytes(raw)
    newline = _detect_newline(text)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, newline, None


def _symlink_guard(file_path):
    """软链接保护：写入软链接会用普通文件顶掉链接节点（os.replace 语义），故直接拒绝。

    返回错误文案；不是软链接（或无法判断）返回 None。真实路径一并给出，便于调用方改道。
    """
    if not file_path:
        return None
    try:
        if not os.path.islink(file_path):
            return None
        real = os.path.realpath(file_path)
    except OSError:
        return None
    return (f"{file_path} 是软链接，拒绝写入（继续写会把链接本身替换成普通文件）；"
            f"请改用真实路径：{real}")


def write_file(file_path: str, text: str, encoding: str = "utf-8", newline: str = "\n"):
    """原子写回文件（先编码校验，再写临时文件并 os.replace）；返回错误信息或 None。

    目标是软链接时直接拒绝，理由见 _symlink_guard；写回会保留原文件权限位，
    避免 Linux 下编辑脚本丢掉可执行位（0755 -> 0644）。
    """
    link_err = _symlink_guard(file_path)
    if link_err:
        return link_err
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    data = normalized if newline in (None, "", "\n") else normalized.replace("\n", newline)
    enc = encoding or "utf-8"
    try:
        payload = data.encode(enc)
    except (UnicodeEncodeError, LookupError) as e:
        return f"内容无法用原编码 {enc} 表示，已放弃写入 - {e}"
    mode = None
    try:
        mode = os.stat(file_path).st_mode & 0o7777
    except OSError:
        mode = None                      # 新建文件：沿用系统默认权限
    tmp = f"{file_path}.tmp{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, file_path)
    except OSError as e:
        try:
            os.chmod(tmp, 0o666)         # 临时文件可能已套上只读权限，先放开再删
        except OSError:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        return f"写入失败 - {e}"
    return None


def fetch_url(url: str, timeout: int = 30):
    """下载 http(s) 文本，返回 (text, encoding, err)；自动探测编码，统一 '\\n'。"""
    try:
        with urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as e:
        return None, None, f"下载失败 - {e}"
    if _looks_binary(raw):
        return None, None, f"二进制内容不支持按文本处理（{len(raw)} bytes）"
    text, enc = _decode_bytes(raw)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, None


DEFAULT_LIMIT = 400


DIFF_LIMIT = 120          # 超过此行数的 diff 折叠为 hunk 索引


DIFF_HUNKS = 20           # 折叠模式下最多列出的 hunk 数


def _short_path(path: str) -> str:
    return os.path.basename(path or "") or (path or "")


def _num_width(total: int) -> int:
    return max(2, len(str(max(total, 1))))


def _numbered(lines: List[str], start: int, end: int) -> str:
    """按绝对行号渲染 [start, end]（1-based，含端点）"""
    w = _num_width(end)
    return "\n".join(f"{i:>{w}}: {lines[i - 1]}" for i in range(start, end + 1)
                     if 1 <= i <= len(lines))


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _split_lines(code: str):
    trailing = code.endswith("\n")
    lines = code.split("\n")
    if trailing and lines and lines[-1] == "":
        lines.pop()
    return lines, trailing


def _join_lines(lines: List[str], trailing: bool) -> str:
    text = "\n".join(lines)
    if trailing and text:
        text += "\n"
    return text


def _new_lines(new_code: str) -> List[str]:
    if not new_code:
        return []
    lines = new_code.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _make_diff(before: str, after: str, context=None):
    """生成 unified diff，返回 (text, truncated, hunks)

    context=None（默认）：3 行上下文；小改动给完整 diff，超过 DIFF_LIMIT 行时只给
    hunk 索引与增删行数，避免刷屏。
    context 显式给出：按指定行数输出，且不折叠（大范围重构可完整核对）。
    """
    n = 3 if context is None else context
    raw = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                    fromfile="before", tofile="after", lineterm="", n=n))
    if not raw:
        return "", False, 0
    hunks, cur = [], None
    for line in raw[2:]:
        if line.startswith("@@"):
            cur = [line]
            hunks.append(cur)
        elif cur is not None:
            cur.append(line)
    if context is not None:
        return "\n".join(raw), False, len(hunks)
    body_total = len(raw) - 2
    if body_total <= DIFF_LIMIT:
        return "\n".join(raw), False, len(hunks)

    rows = []
    for h in hunks[:DIFF_HUNKS]:
        add = sum(1 for l in h[1:] if l.startswith("+"))
        rem = sum(1 for l in h[1:] if l.startswith("-"))
        rows.append(f"{h[0]}  -{rem}/+{add}")
    text = "\n".join(rows)
    if len(hunks) > DIFF_HUNKS:
        text += f"\n... 另有 {len(hunks) - DIFF_HUNKS} 处改动"
    text += f"\n(diff 共 {body_total} 行，已折叠为 hunk 索引；可用 read + start_line 查看)"
    return text, True, len(hunks)
