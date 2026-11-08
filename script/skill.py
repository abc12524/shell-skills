# skill: script
# description: 脚本/代码增删改查（CRUD）+ 结构骨架（改代码首选）
# status: enable
# usage:
#   action = read | outline | create | add | edit | delete | patch | rename_symbol
#   数据源: file_path（可写）/ url（只读）；read 缺省给骨架，pattern 按符号名/正则检索
#   定位: symbol / start_line+end_line / old_code / block+start_line（最小语法块）
#   position（配 symbol/block）: add=start|end|before|after；edit=before|after，省略整段替换
#   写动作回 unified diff，dry_run 只预览；缩进用 @n:；new_code 别名 content
#   一致性: read 返回 file_id，写动作传 expected_sha256 可拒绝过期编辑
#   批量: replacements 按序执行、全成或全败；capture/paste 为单次调用内寄存器
#   rename_symbol: 配 symbol+new_name；patch: files=[{file_path,edits,rename_to}]
#   校验: validate=auto|on|off；写目标含 :1-20 或 ; 会被拒绝；strip_prefixes 默认剥离行号
#   可选: with_calls、diff_context、deadline_ms、encoding、language、replace_all、limit
#   例: {"action":"edit","file_path":"a.py","symbol":"parse","new_code":"..."}
from ._script_impl import script_editor

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import error

# 入口接受的参数键；拼错/臆造的键直接报错（历史上 action=create 传 content 会静默写空文件）
_ARGS = (
    "action", "file_path", "url", "language", "symbol", "pattern",
    "start_line", "end_line", "limit", "old_code", "new_code", "content",
    "replace_all", "dry_run", "position", "with_calls", "diff_context",
    "replacements", "expected_sha256", "validate", "deadline_ms", "encoding",
    "files", "rename_to", "new_name", "edits", "block", "strip_prefixes",
)


def run(arguments):
    a = arguments or {}
    unknown = [k for k in a if k not in _ARGS]
    if unknown:
        return error(
            "未知参数 " + "、".join(unknown) + "；可用参数: " + "、".join(_ARGS),
            code="unknown_argument")
    new_code = a.get("new_code")
    if new_code is None:
        new_code = a.get("content")          # content 是 new_code 的别名
    return script_editor(
        action=a.get("action", ""),
        file_path=a.get("file_path"),
        url=a.get("url"),
        language=a.get("language"),
        symbol=a.get("symbol"),
        pattern=a.get("pattern"),
        start_line=a.get("start_line"),
        end_line=a.get("end_line"),
        limit=a.get("limit", 400),
        old_code=a.get("old_code"),
        new_code=new_code,
        replace_all=a.get("replace_all", False),
        dry_run=a.get("dry_run", False),
        position=a.get("position"),
        with_calls=a.get("with_calls", False),
        diff_context=a.get("diff_context"),
        replacements=a.get("replacements"),
        expected_sha256=a.get("expected_sha256"),
        validate=a.get("validate"),
        deadline_ms=a.get("deadline_ms"),
        encoding=a.get("encoding"),
        files=a.get("files"),
        rename_to=a.get("rename_to"),
        new_name=a.get("new_name"),
        edits=a.get("edits"),
        block=a.get("block", False),
        strip_prefixes=a.get("strip_prefixes", True),
    )