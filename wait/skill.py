# skill: wait
# description: 等待指定秒数（阻塞），最长 10 分钟（600 秒）
# status: enable
# usage:
#   arguments.seconds = 等待秒数，支持小数（默认 1，范围 0~600；超过 600 自动截断为 600）
#   示例: {"seconds": 5}
#   示例: {"seconds": 0.5}
import time

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

MAX_SECONDS = 600.0


def run(arguments):
    a = arguments or {}
    raw = a.get("seconds", 1)

    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return error(f"seconds 必须是数字，收到: {raw!r}", code="bad_request")

    if seconds < 0:
        return error(f"seconds 不能为负: {seconds}", code="bad_request")

    requested = seconds
    capped = False
    if seconds > MAX_SECONDS:
        seconds = MAX_SECONDS
        capped = True

    time.sleep(seconds)

    return ok({
        "requested_seconds": requested,
        "seconds": seconds,
        "capped": capped,
        "max_seconds": MAX_SECONDS,
    })


if __name__ == "__main__":
    import sys

    secs = sys.argv[1] if len(sys.argv) > 1 else 1
    print(run({"seconds": secs}))
