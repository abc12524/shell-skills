"""文本层：编码/换行探测、文件与 URL 读写、行切分与编号渲染、unified diff。

不含语言语法知识与定位决策，只提供「把文件读成行、把行写回文件」这一层的原语。
"""

from urllib.request import urlopen
import difflib
import hashlib
import os
import tempfile

from typing import List, Optional

_ENVELOPE_OK = None

_EXT_LANG = {
    ".py": "python", ".pyw": "python",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".php": "php", ".phtml": "php",
    ".swift": "swift",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
}


def infer_language(file_path: str) -> Optional[str]:
    """按扩展名推断语言（用于 file_path 模式自动补齐 language）"""
    return _EXT_LANG.get(os.path.splitext(file_path or "")[1].lower())


# BOM 必须最先判断：UTF-16/32 文本天然含 \x00，二进制嗅探会把它们误杀。
_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def _bom_encoding(raw: bytes) -> Optional[str]:
    for prefix, enc in _BOMS:
        if raw.startswith(prefix):
            return enc
    return None


def _looks_binary(raw: bytes) -> bool:
    if not raw:
        return False
    if _bom_encoding(raw):          # UTF-16/32 的零字节不算二进制
        return False
    chunk = raw[:8192]
    if b"\x00" in chunk:
        return True
    text_chars = bytearray({7, 8, 9, 10, 11, 12, 13, 27, 0x81, 0x8d, 0x8f, 0x90, 0x9d})
    odd = sum(1 for b in chunk if b < 32 and b not in text_chars)
    return bool(chunk) and odd / float(len(chunk)) > 0.30


_CTRL_OK = "\r\n\t"
_CJK_RANGES = ((0x3000, 0x303F), (0x4E00, 0x9FFF), (0x3400, 0x4DBF),
               (0xFF00, 0xFFEF), (0x20000, 0x2FA1F))


def _text_score(raw: bytes, text: str, enc: str) -> float:
    """候选编码打分：往返编码一致优先，其次惩罚控制字符/私用区/替换符。"""
    score = 0.0
    try:
        if text.encode(enc) == raw:
            score += 100.0
    except (UnicodeEncodeError, LookupError):
        pass
    bad = 0
    for ch in text:
        cp = ord(ch)
        if ch in _CTRL_OK or 32 <= cp <= 126 or any(lo <= cp <= hi for lo, hi in _CJK_RANGES):
            continue
        if cp < 32 or cp == 0x7F or 0xE000 <= cp <= 0xF8FF or cp == 0xFFFD:
            bad += 1
    score -= bad * 5.0
    return score


def _decode_bytes(raw: bytes, preferred: str = None):
    """自动探测编码并解码，返回 (text, encoding)。latin-1 兜底不会失败。

    preferred（来自 config / 调用方）优先尝试；否则按 utf-8 -> gb18030 -> big5
    依次评分，避免「先匹配到能解码但产生乱码」的编码。
    """
    hint = _bom_encoding(raw)
    if hint:
        try:
            return raw.decode(hint), hint
        except UnicodeDecodeError:
            pass
    candidates: List[str] = []
    if preferred:
        candidates.append(preferred)
    for enc in ("utf-8", "gb18030", "big5"):
        if enc not in candidates:
            candidates.append(enc)
    best = None
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _text_score(raw, text, enc)
        if best is None or score > best[0]:
            best = (score, text, enc)
    if best:
        return best[1], best[2]
    return raw.decode("latin-1"), "latin-1"


def _detect_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def read_file(file_path: str, max_bytes: int = None, encoding: str = None):
    """读取本地文件，返回 (text, encoding, newline, err)。

    text 统一为 '\\n' 换行，newline 记录原文件换行风格（写入时还原）。
    max_bytes 给出时，超过上限直接报错（调用方可用 read_file_head 取头部）。
    """
    if os.path.isdir(file_path):
        return None, None, None, f"路径是目录: {file_path}"
    try:
        if max_bytes:
            size = os.path.getsize(file_path)
            if size > max_bytes:
                return None, None, None, (
                    f"文件过大（{size} bytes，上限 {max_bytes}）；"
                    f"可调大 SCRIPT_MAX_BYTES 或改用其他方式分段处理")
        with open(file_path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, None, None, f"文件不存在: {file_path}"
    except OSError as e:
        return None, None, None, f"读取失败 - {e}"
    if _looks_binary(raw):
        return None, None, None, f"二进制文件不支持按文本处理: {file_path}（{len(raw)} bytes）"
    text, enc = _decode_bytes(raw, encoding)
    newline = _detect_newline(text)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, newline, None


def read_file_head(file_path: str, max_bytes: int, encoding: str = None):
    """读取文件头部（max_bytes 内，截断到最后一个换行），返回 (text, encoding, err)。"""
    try:
        with open(file_path, "rb") as f:
            raw = f.read(max_bytes)
    except FileNotFoundError:
        return None, None, f"文件不存在: {file_path}"
    except OSError as e:
        return None, None, f"读取失败 - {e}"
    cut = raw.rfind(b"\n")
    if cut > 0:
        raw = raw[:cut]
    if _looks_binary(raw):
        return None, None, f"二进制文件不支持按文本处理: {file_path}"
    text, enc = _decode_bytes(raw, encoding)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, None


def file_sha256(file_path: str) -> Optional[str]:
    """整文件内容哈希（读取原始字节，不受编码/换行归一影响）；不存在返回 None。"""
    try:
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def file_identity(file_path: str) -> dict:
    """文件一致性标识：sha256 + size + mtime_ns，读后编辑时用来检测外部修改。"""
    try:
        stat = os.stat(file_path)
        return {"sha256": file_sha256(file_path), "size": stat.st_size,
                "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))}
    except OSError:
        return {}


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

    临时文件用 mkstemp 在同目录创建（O_EXCL，文件名不可预测），避免预置同名符号
    链接导致的 TOCTOU 写入。目标是软链接时直接拒绝，理由见 _symlink_guard；写回
    会保留原文件权限位，避免 Linux 下编辑脚本丢掉可执行位（0755 -> 0644）。
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
    parent = os.path.dirname(os.path.abspath(file_path)) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        return f"创建目录失败 - {e}"
    tmp = None
    fd = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(file_path) + ".",
                                   suffix=".tmp", dir=parent)
        if mode is not None and hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, file_path)
        tmp = None
    except OSError as e:
        return f"写入失败 - {e}"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return None


def fetch_url(url: str, timeout: int = 30, max_bytes: int = DEFAULT_MAX_BYTES,
              encoding: str = None):
    """下载 http(s) 文本，返回 (text, encoding, err)；自动探测编码，统一 '\\n'。"""
    try:
        with urlopen(url, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1 if max_bytes else -1)
    except Exception as e:
        return None, None, f"下载失败 - {e}"
    if max_bytes and len(raw) > max_bytes:
        return None, None, f"响应过大（超过 {max_bytes} bytes），已中止"
    if _looks_binary(raw):
        return None, None, f"二进制内容不支持按文本处理（{len(raw)} bytes）"
    text, enc = _decode_bytes(raw, encoding)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, None


DEFAULT_LIMIT = 400

DIFF_LIMIT = 120          # 超过此行数的 diff 折叠为 hunk 索引

DIFF_HUNKS = 20           # 折叠模式下最多列出的 hunk 数


def _short_path(path: str, cwd: str = None) -> str:
    """展示用路径：优先相对当前目录并保留层级，不在当前目录下才退回 basename。"""
    if not path:
        return ""
    try:
        base = os.path.abspath(cwd or os.getcwd())
        absolute = os.path.abspath(path)
        rel = os.path.relpath(absolute, base)
    except (OSError, ValueError):
        return os.path.basename(path) or path
    if rel == ".." or rel.startswith(".." + os.sep):
        return os.path.basename(path) or path
    return rel.replace(os.sep, "/")


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


def _make_diff(before: str, after: str, context=None, limit: int = None,
               hunks: int = None):
    """生成 unified diff，返回 (text, truncated, hunks)

    context=None（默认）：3 行上下文；小改动给完整 diff，超过 DIFF_LIMIT 行时只给
    hunk 索引与增删行数，避免刷屏。limit/hunks 可覆盖模块阈值（SCRIPT_DIFF_LIMIT /
    SCRIPT_DIFF_HUNKS 环境变量在导入时生效）。
    context 显式给出：按指定行数输出，且不折叠（大范围重构可完整核对）。
    """
    n = 3 if context is None else context
    limit = DIFF_LIMIT if limit is None else limit
    hunks_cap = DIFF_HUNKS if hunks is None else hunks
    raw = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                    fromfile="before", tofile="after", lineterm="", n=n))
    if not raw:
        return "", False, 0
    hunk_rows, cur = [], None
    for line in raw[2:]:
        if line.startswith("@@"):
            cur = [line]
            hunk_rows.append(cur)
        elif cur is not None:
            cur.append(line)
    if context is not None:
        return "\n".join(raw), False, len(hunk_rows)
    body_total = len(raw) - 2
    if body_total <= limit:
        return "\n".join(raw), False, len(hunk_rows)

    rows = []
    for h in hunk_rows[:hunks_cap]:
        add = sum(1 for l in h[1:] if l.startswith("+"))
        rem = sum(1 for l in h[1:] if l.startswith("-"))
        rows.append(f"{h[0]}  -{rem}/+{add}")
    text = "\n".join(rows)
    if len(hunk_rows) > hunks_cap:
        text += f"\n... 另有 {len(hunk_rows) - hunks_cap} 处改动"
    text += f"\n(diff 共 {body_total} 行，已折叠为 hunk 索引；可用 read + start_line 查看)"
    return text, True, len(hunk_rows)