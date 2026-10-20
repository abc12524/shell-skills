# skill: script
# description: 脚本/代码增删改查（CRUD）+ 结构骨架（改代码首选）
# status: enable
# usage:
#   action = read | outline | create | add | edit | delete（file_path 可写 / url 只读）
#   定位三选一：symbol（可裸方法名）/ start_line+end_line（add 时 0=开头）/
#     old_code 文本锚点（精确→忽略缩进→忽略标点；多处配 replace_all=true）
#   position（配 symbol）：add 用 start|end|before|after（默认 end=符号体内末尾，
#     要在符号之后加新函数用 after）；edit 仅 before|after，省略 position = 整段替换符号；
#     体内插内容用 add
#   read 按 symbol/pattern/行范围取内容（缺省给骨架）；outline 给骨架（with_calls 附调用）
#   pattern 先按符号名正则，未命中再按行检索（不是直接对源码 grep）
#   create 新建；写动作回 unified diff（默认 3 行上下文，过长折叠为 hunk），dry_run 只预览；
#     diff_context=N 显式指定上下文行数并输出完整 diff（不折叠）；软链接报错 is_symlink
#     大改动改完建议再 outline 复核结构
#   插入定义类内容自动补空行（Python 顶层 2 行、其余 1 行）
#   删除：symbol / 模糊匹配会收敛接缝空行（最多 2 行）；精确 old_code 严格按内容删
#   缩进用 @n:（相对插入基准缩进：@0=基准层，@1=深一层，可为负），勿混写字面空格
#   超 limit 截断给 next；old_code 未命中提示最近行号
#   例：{"action":"edit","file_path":"a.py","symbol":"parse","new_code":"..."}
#
# 提示：改代码优先用本工具，先 outline → read → dry_run → 写；新建文件用 create。
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
