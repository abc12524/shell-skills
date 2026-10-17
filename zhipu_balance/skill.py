# skill: zhipu_balance
# description: 智谱 BigModel 账户余额查询
# status: enable
# usage:
#   arguments.format = text | json（默认 text；json 附带 raw 原始返回）
#   配置: 同目录 config.json —— ZHIPU_API_KEY
#   示例: {} 或 {"format": "json"}
"""智谱 BigModel 账户余额查询 skill（由 balance_checker.py 的 zhipu 部分拆分改造）。

余额走控制台接口 query-customer-account-report（主接口，失败即整体失败）；
用量限额走 monitor/usage/quota/limit（可选，未订阅 Coding Plan 时报错属正常）。
原脚本的多厂商分发、ANSI 着色、dotenv/CLI 参数等已剥离，改为标准 skill 信封。
"""
import json
import os

import requests

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

ZHIPU_BALANCE_URL = "https://www.bigmodel.cn/api/biz/account/query-customer-account-report"
ZHIPU_QUOTA_URL = "https://www.bigmodel.cn/api/monitor/usage/quota/limit"
DEFAULT_TIMEOUT = 10


class ZhipuError(Exception):
    """查询失败：message 面向用户，code 为稳定短标识。"""

    def __init__(self, message, code="query_failed"):
        super().__init__(message)
        self.code = code


def _load_config():
    """把本技能文件夹内的 config.json 注入环境变量（不覆盖已有 env）。

    配置与脚本同目录：zhipu_balance/config.json（模板见 config.example.json），键为环境变量名。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(cfg, dict):
        for key, value in cfg.items():
            if value and not os.environ.get(key):
                os.environ[key] = str(value)


def _to_float(value):
    """把接口返回的字符串/数字安全转成 float。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value, unit=""):
    """格式化金额，带千分位。"""
    num = _to_float(value)
    if num is None:
        return "-"
    return f"{num:,.2f}{unit}"


def _zhipu_headers(api_key, bearer=False):
    token = f"Bearer {api_key}" if bearer else api_key
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "shell-skills/zhipu_balance",
    }


def _zhipu_get(url, api_key, timeout):
    """依次尝试裸 key 与 Bearer 前缀两种鉴权写法；成功返回 payload。"""
    last_error = "未知错误"
    for bearer in (False, True):
        try:
            resp = requests.get(
                url, headers=_zhipu_headers(api_key, bearer), timeout=timeout
            )
        except requests.RequestException as exc:
            last_error = f"网络异常: {exc}"
            continue

        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            continue

        try:
            payload = resp.json()
        except ValueError:
            last_error = f"返回非 JSON: {resp.text[:200]}"
            continue

        code = payload.get("code")
        if code == 200 or payload.get("success") is True:
            return payload

        msg = payload.get("msg") or payload.get("message") or "未提供错误信息"
        if code in (401, 1000, 1001, 1002, 1003, 1004):
            raise ZhipuError(f"鉴权失败 [{code}] {msg}，请检查 ZHIPU_API_KEY", code="auth_failed")

        # 该接口可能返回嵌套的 {code,msg} 错误体（例如 {code:500, msg:{code:50011,...}}），
        # 需要展开取出内层真正的错误码与描述
        inner_code = None
        if isinstance(msg, dict):
            inner_code = msg.get("code")
            msg = msg.get("message") or msg.get("msg") or json.dumps(msg, ensure_ascii=False)

        # 未订阅 Coding Plan 属于正常状态，不是故障
        if str(inner_code) == "50011" or "coding plan" in str(msg).lower():
            raise ZhipuError(
                "未订阅 GLM Coding Plan（该账号无此套餐，属正常状态）",
                code="no_coding_plan",
            )

        raise ZhipuError(f"智谱返回错误 [{inner_code or code}] {str(msg)[:150]}")

    raise ZhipuError(last_error)


def _extract_limits(quota_data):
    """从配额接口返回值里挑出真正表示限额的列表。"""
    for item in (quota_data.get("limits"), quota_data.get("data"), quota_data.get("list")):
        if isinstance(item, list) and item:
            return [x for x in item if isinstance(x, dict)]
    return []


def query_zhipu(api_key, timeout=DEFAULT_TIMEOUT):
    """查询智谱账户余额；配额限额为尽力而为，缺失不影响结果。"""
    balance_data = {}
    quota_data = {}
    warnings = []
    quota_ok = False

    # 1) 账户余额（主接口，失败则整体失败）
    try:
        payload = _zhipu_get(ZHIPU_BALANCE_URL, api_key, timeout)
        balance_data = payload.get("data") or payload.get("result") or {}
    except ZhipuError as exc:
        if exc.code == "auth_failed":
            raise
        raise ZhipuError(f"智谱余额接口不可用: {exc}", code=exc.code) from exc

    if not balance_data:
        raise ZhipuError("智谱余额接口返回数据为空")

    # 2) 用量/配额限额（可选接口：未订阅 Coding Plan 时会报错，属正常）
    try:
        payload = _zhipu_get(ZHIPU_QUOTA_URL, api_key, timeout)
        quota_data = payload.get("data") or payload.get("result") or {}
        quota_ok = bool(quota_data)
    except ZhipuError as exc:
        if exc.code == "auth_failed":
            raise
        if exc.code != "no_coding_plan":
            warnings.append(f"配额信息不可用: {exc}")

    return {
        "balance": _to_float(
            balance_data.get("balance")
            if balance_data.get("balance") is not None
            else balance_data.get("availableBalance")
        ),
        "available_balance": _to_float(balance_data.get("availableBalance")),
        "recharge_amount": _to_float(balance_data.get("rechargeAmount")),
        "give_amount": _to_float(balance_data.get("giveAmount")),
        "total_spend_amount": _to_float(balance_data.get("totalSpendAmount")),
        "frozen_balance": _to_float(balance_data.get("frozenBalance")),
        "currency": "CNY",
        "limits": _extract_limits(quota_data),
        "quota_available": quota_ok,
        "warnings": warnings,
        "raw": {"balance": balance_data, "quota": quota_data},
    }


def _render(data):
    """精简为一行：可用余额（缺失回退账户余额，耗尽时提示充值）。"""
    avail = data.get("available_balance")
    if avail is None:
        avail = data.get("balance")
    if avail is None:
        return "智谱 BigModel 可用余额 未知"
    line = f"智谱 BigModel 可用余额 {_fmt(avail, ' 元')}"
    if avail <= 0:
        line += "（已耗尽，请充值）"
    return line


def run(arguments):
    """skill 入口：arguments(dict) → ok()/error() 信封。"""
    a = arguments or {}

    fmt = str(a.get("format") or "text").strip().lower()
    if fmt not in ("text", "json"):
        return error(f"arguments.format 只能是 text / json，收到 '{fmt}'", code="bad_request")

    try:
        timeout = int(a.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return error("arguments.timeout 必须是整数", code="bad_request")

    _load_config()
    api_key = str(a.get("api_key") or os.environ.get("ZHIPU_API_KEY", "")).strip()
    if not api_key:
        return error(
            "缺少智谱凭证（ZHIPU_API_KEY），请配置同目录 config.json",
            code="not_configured",
        )

    try:
        data = query_zhipu(api_key, timeout)
    except ZhipuError as exc:
        return error(str(exc), code=exc.code)
    except Exception as exc:  # 兜底
        return error(f"{type(exc).__name__}: {exc}", code="query_failed")

    result = {"vendor": "zhipu", **{k: v for k, v in data.items() if k != "raw"}}
    if fmt == "json":
        result["raw"] = data.get("raw")
    else:
        result["text"] = _render(data)
    return ok(result)


if __name__ == "__main__":  # 本地调试：python skill.py '{"format": "json"}'
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
