# skill: script
# description: 脚本/代码增删改查（CRUD）+ 结构骨架（改代码首选）
# status: enable
# usage:
#   action = read | outline | create | add | edit | delete
#   来源：file_path（本地，可写）或 url（http/https，只读）
#   定位（add/edit/delete 互斥三选一）：symbol 符号名（可裸方法名）/ start_line 行号（配 end_line；
#     add 时 0 = 文件开头）/ old_code 文本锚点（逐字符一致，多处需 replace_all=true）
#   position（配 symbol）：start|end = 符号体内首/末（add 默认 end）；before|after = 符号前/后
#   read 取内容（symbol / pattern / 行范围，省略则给骨架）；outline 骨架（with_calls 附调用）
#   create 新建；写入类回 unified diff（过长折叠），dry_run=true 只预览
#   new_code 按相对缩进 0 写；定义类内容自动隔 1 空行；软链接直接报错 is_symlink；
#   超 limit 截断给 next 续读；old_code 未命中有「最接近行号」提示
#   例：{"action":"edit","file_path":"a.py","symbol":"parse","new_code":"..."}
#
# 提示：改代码优先用本工具（别用 sed / str.replace 手搓），先 outline → read 取行号 → dry_run 预览；
#   新建文件用 create，不要用 add。
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
    )
