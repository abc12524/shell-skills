# skill: skill_guide
# description: 如何编写 skill：规范、要求、头部格式与调用约定（文档见同目录 skill_README.md）
# status: enable
# usage:
#   arguments.action = read | path（默认 read）
#     read  返回说明书 Markdown 全文
#     path  只返回说明书的绝对路径
#   说明: 文档来源为同目录 skill_README.md，内容与 android-agent 内置的 skill 说明保持一致。
#         修改规范时请同时更新文档与本技能。
#   示例: {"action": "read"}
#   示例: {"action": "path"}
import json
import os
import sys

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

DOC_NAME = "skill_README.md"


def _doc_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DOC_NAME)


def run(arguments):
    a = arguments or {}
    action = str(a.get("action") or "read").strip().lower()

    path = _doc_path()
    if not os.path.isfile(path):
        return error("说明书不存在: %s" % path, code="not_found")

    if action == "path":
        return ok({"path": path})

    if action != "read":
        return error("未知 action '%s'，可用: read / path" % action,
                     code="unknown_action")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        return error("读取说明书失败: %s" % exc, code="io_error")

    return ok({
        "path": path,
        "name": DOC_NAME,
        "lines": len(content.splitlines()),
        "content": content,
    })


if __name__ == "__main__":  # 本地调试：python skill.py '{"action":"read"}'
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
