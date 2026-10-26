# skill: script
# description: 脚本/代码增删改查（CRUD）+ 结构骨架（改代码首选）
# status: enable
# usage:
#   action = read | outline | create | add | edit | delete（file_path 可写 / url 只读）
#   定位（三选一）: symbol（可裸方法名）/ start_line+end_line（add 时 0=开头）/
#     old_code 文本锚点（精确→忽略缩进→忽略标点；多处配 replace_all=true）
#   position（配 symbol）: add 用 start|end|before|after（默认 end=符号体末尾；符号后用 after）；
#     edit 仅 before|after，省略 = 整段替换；体内插入用 add
#   read/pattern 取内容（缺省给骨架），outline 看结构（with_calls 附调用）；pattern 先符号名正则再按行检索
#   写动作回 unified diff（默认 3 行上下文，过长折叠；diff_context=N 输出完整 diff），dry_run 只预览
#   缩进用 @n:（相对插入基准，可为负）；超 limit 给 next；old_code 未命中提示最近行号
#   例: {"action":"edit","file_path":"a.py","symbol":"parse","new_code":"..."}
from ._script_impl import script_editor


def run(arguments):
    a = arguments or {}
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
        new_code=a.get("new_code"),
        replace_all=a.get("replace_all", False),
        dry_run=a.get("dry_run", False),
        position=a.get("position"),
        with_calls=a.get("with_calls", False),
        diff_context=a.get("diff_context"),
    )
