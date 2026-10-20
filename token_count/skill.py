# skill: token_count
# description: 统计 DeepSeek V4 token 数（直接文本或文件）
# usage:
#   arguments.text = 直接输入的文本（与 file_path 二选一）
#   arguments.file_path = 文件路径（优先于 text）
#   arguments.encoding = 文件编码（可选，默认自动探测：utf-8-sig → gb18030）
#   示例: {"text": "Hello!"}
#   示例: {"file_path": "path/to/file.txt"}
import json
import os

try:
    from _envelope import ok, error
except ImportError:
    from core.tools.envelope import ok, error

try:
    from ._tokenizer import count_tokens, DEFAULT_TOKENIZER
except ImportError:
    from _tokenizer import count_tokens, DEFAULT_TOKENIZER

FALLBACK_ENCODINGS = ("utf-8-sig", "gb18030")


def _tokenizer_name():
    path = (os.environ.get("TOKEN_COUNT_TOKENIZER")
            or os.environ.get("TOKENIZER_PATH")
            or DEFAULT_TOKENIZER)
    return os.path.basename(path)


def _read_file(path, encoding=None):
    with open(path, "rb") as f:
        data = f.read()
    if encoding:
        return data.decode(encoding), encoding
    for enc in FALLBACK_ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def run(arguments):
    a = arguments or {}
    if not isinstance(a, dict):
        return error("arguments 必须是对象", code="bad_request")

    file_path = a.get("file_path")
    has_text = "text" in a
    text = a.get("text")

    if file_path is not None and not isinstance(file_path, str):
        return error("file_path 必须是字符串", code="bad_request")
    if has_text and text is not None and not isinstance(text, str):
        return error("text 必须是字符串", code="bad_request")

    if not file_path and not has_text:
        return error("需要提供 text 或 file_path", code="missing_argument")

    source = "file" if file_path else "text"
    encoding = None
    if file_path:
        path = os.path.expanduser(file_path)
        if not os.path.isfile(path):
            return error(f"文件不存在: {file_path}", code="bad_request")
        try:
            text, encoding = _read_file(
                path, (a.get("encoding") or "").strip() or None)
        except (OSError, LookupError, UnicodeDecodeError) as exc:
            return error(f"读取文件失败: {exc}", code="bad_request")
    elif text is None:
        text = ""

    try:
        tokens = count_tokens(text)
    except Exception as exc:
        return error(f"token 计算失败: {exc}", code="internal")

    result = {
        "tokens": tokens,
        "characters": len(text),
        "source": source,
        "tokenizer": _tokenizer_name(),
    }
    if file_path:
        result["path"] = os.path.abspath(os.path.expanduser(file_path))
        result["encoding"] = encoding
        result["bytes"] = os.path.getsize(result["path"])
    return ok(result)


if __name__ == "__main__":
    print(json.dumps(run(json.loads(os.sys.argv[1]) if len(os.sys.argv) > 1 else {}),
                     ensure_ascii=False))
