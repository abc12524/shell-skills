# skill: llm_balance
# description: 火山引擎 / 智谱 / DeepSeek 余额查询
# status: enable
# usage:
#   arguments.action = all | volc | zhipu | deepseek（默认 all）
#   arguments.timeout = 超时秒数（默认 10）
#   arguments.access_key / secret_key / api_key = 临时覆盖凭证（默认读同目录 config.json）
#   只返回接口原始数据；示例: {} 或 {"action": "deepseek"}
try:  # 作为包导入（shell-tool / 移动端 runner）
    from . import _impl
except ImportError:  # 直接以脚本运行：同目录导入
    import _impl


def run(arguments):
    """skill 入口：arguments(dict) → ok()/error() 信封。"""
    return _impl.emit(arguments)


if __name__ == "__main__":  # 本地调试：python skill.py '{"action": "deepseek"}'
    import json
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
