# skill: script
# description: 脚本/代码增删改查（CRUD）+ 结构骨架（改代码首选）
# status: enable
# usage:
#   action = read | outline | create | add | edit | delete
#            （patch / rename_symbol 是 edit 的快捷方式，仍可用）
#   数据源: path（本地可写 / http(s) 只读）；read 缺省给骨架，pattern 按符号名/正则检索
#   定位: symbol / start_line+end_line / old_code / block+start_line（最小语法块）
#   position（配 symbol/block）: add=start|end|before|after；edit=before|after，省略整段替换
#   edit 批量: edits=[{old_code,new_code}]（全成或全败；capture/paste 单次调用内寄存器）
#   多文件: edit + files=[{path,edits,rename_to}]；重命名: edit + symbol + new_name
#   默认行为: 编码/换行/缩进/空行自动处理；写前语法校验 auto；diff 自动折叠；行号前缀自动剥离
#   一致性: read 返回 file_id，写动作传 expected_sha256 可拒绝过期编辑
#   可选: with_calls、replace_all、dry_run、position、block、validate、rename_to
#   例: {"action":"edit","path":"a.py","symbol":"parse","new_code":"..."}
from ._script_impl import script_editor

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import error

# 模型可见的核心参数；旧名与高级覆盖放在 _ALIAS_ARGS，仍接受但不进错误提示
_CORE_ARGS = (
    "action", "path", "language", "symbol", "pattern", "start_line", "end_line",
    "old_code", "new_code", "replace_all", "dry_run", "position", "with_calls",
    "edits", "files", "rename_to", "new_name", "expected_sha256", "validate", "block",
)
_ALIAS_ARGS = (
    "file_path", "url", "content", "replacements", "limit", "diff_context",
    "deadline_ms", "strip_prefixes", "encoding",
)
_ARGS = _CORE_ARGS + _ALIAS_ARGS


def run(arguments):
    a = arguments or {}
    unknown = [k for k in a if k not in _ARGS]
    if unknown:
        return error(
            "未知参数 " + "、".join(unknown) + "；核心参数: " + "、".join(_CORE_ARGS),
            code="unknown_argument")
    new_code = a.get("new_code")
    if new_code is None:
        new_code = a.get("content")          # content 是 new_code 的别名
    path = a.get("path")
    file_path = a.get("file_path")
    url = a.get("url")
    if path and (file_path or url):
        return error("path 与 file_path/url 互斥", code="conflicting_target")
    if not path:
        path = file_path or url
        file_path = None
        url = None
    edits = a.get("edits")
    if edits is None:
        edits = a.get("replacements")        # replacements 是 edits 的别名
    return script_editor(
        action=a.get("action", ""),
        path=path,
        file_path=file_path,
        url=url,
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
        deadline_ms=a.get("deadline_ms", 30000),
        encoding=a.get("encoding"),
        files=a.get("files"),
        rename_to=a.get("rename_to"),
        new_name=a.get("new_name"),
        edits=edits,
        block=a.get("block", False),
        strip_prefixes=a.get("strip_prefixes", True),
    )