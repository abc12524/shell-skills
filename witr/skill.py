# skill: witr
# description: witr 命令速查（进程 / PID / 端口 / 文件 / 容器溯源），纯文档不执行命令
# status: enable
# usage:
#   witr <名称> | witr --port 8080 | witr --pid 1234 | witr --file /var/log/app.log
#   arguments.action = guide（默认，全文）| path（只返回路径）
#   arguments.topic  = 只取一节，模糊匹配标题（如 "port" / "exit"）
#   示例: {"topic":"port"}
"""witr 使用说明技能（纯文档型）。

设计取舍：
- 不执行 witr，也不检测本机是否安装 —— 说明文档对「Linux 远端有 witr、本地没有」
  的场景同样有效，命令由 ssh / shell 去跑。
- 文档按 `## ` 二级标题切节，`topic` 用大小写不敏感的子串匹配标题；命中多节时按
  原文顺序拼接。命中为空则报 not_found 并回传可用标题，方便上层换词重试。
- 输出只有一份 `content`，不额外返回 titles/markdown/html 等重复形态。
"""
import os
import sys

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

DOC_NAME = "witr_README.md"
ACTIONS = ("guide", "read", "path")


def _doc_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DOC_NAME)


def _split_sections(text):
    """按 `## ` 切节，返回 [(仅标题词, 原文块)]；首个二级标题之前的内容归入 ('', 块)。"""
    sections, title, buf = [], "", []

    def flush():
        block = "\n".join(buf).strip()
        if title or block:
            sections.append((title, block))

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            title, buf = line[3:].strip(), [line]
        else:
            buf.append(line)
    flush()
    return sections


def run(arguments):
    a = arguments if isinstance(arguments, dict) else {}
    action = str(a.get("action") or "guide").strip().lower()
    topic = str(a.get("topic") or "").strip()

    path = _doc_path()
    if not os.path.isfile(path):
        return error("说明书不存在: %s" % path, code="not_found")

    if action == "path":
        return ok({"path": path})

    if action not in ("guide", "read"):
        return error("未知 action '%s'，可用: %s" % (action, " / ".join(ACTIONS)),
                     code="unknown_action")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return error("读取说明书失败: %s" % exc, code="io_error")

    if not topic:
        return ok({"path": path, "topic": "", "content": text})

    low = topic.lower()
    hits = [(t, b) for t, b in _split_sections(text) if t and low in t.lower()]
    if not hits:
        titles = [t for t, _ in _split_sections(text) if t]
        return error("没有匹配 '%s' 的章节，可用章节: %s" % (topic, " | ".join(titles)),
                     code="not_found")

    return ok({"path": path, "topic": topic, "content": "\n\n".join(b for _, b in hits)})


if __name__ == "__main__":  # 本地调试：python skill.py '{"topic":"port"}'
    import json
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
