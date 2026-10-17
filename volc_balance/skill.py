# skill: volc_balance
# description: 火山引擎账户余额查询
# status: enable
# usage:
#   arguments.format = text | json（默认 text；json 附带 raw 原始返回）
#   配置: 同目录 config.json —— VOLC_ACCESS_KEY / VOLC_SECRET_KEY（可选 VOLC_REGION）
#   示例: {} 或 {"format": "json"}
"""火山引擎账户余额查询 skill（由 balance_checker.py 的 volc 部分拆分改造）。

调用火山官方 OpenAPI billing/QueryBalanceAcct 查询资金账户余额，需要 AK/SK 签名。
原脚本的多厂商分发、ANSI 着色、dotenv/CLI 参数等已剥离，改为标准 skill 信封。
"""
import json
import os

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

# 火山引擎 SDK 为必需依赖，缺失时在 run() 里以可读错误返回（不在导入期退出）
_VOLC_IMPORT_ERROR = None
try:
    from volcengine.ApiInfo import ApiInfo
    from volcengine.base.Service import Service
    from volcengine.Credentials import Credentials
    from volcengine.ServiceInfo import ServiceInfo
except ImportError as exc:  # pragma: no cover
    _VOLC_IMPORT_ERROR = str(exc)
    ApiInfo = Service = Credentials = ServiceInfo = None  # type: ignore

VOLC_HOST = "open.volcengineapi.com"
VOLC_SERVICE = "billing"
VOLC_API_VERSION = "2022-01-01"
VOLC_DEFAULT_REGION = "cn-beijing"
DEFAULT_TIMEOUT = 10


class BalanceError(Exception):
    """余额查询失败：message 面向用户。"""


def _load_config():
    """把本技能文件夹内的 config.json 注入环境变量（不覆盖已有 env）。

    配置与脚本同目录：volc_balance/config.json（模板见 config.example.json），键为环境变量名。
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


def _sanitize(text, *secrets):
    """从错误信息里抹掉密钥，返回的报文常会原样回显 AK。"""
    redacted = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            redacted = redacted.replace(secret, "***")
    return redacted


def query_volc(access_key, secret_key, region=VOLC_DEFAULT_REGION,
               timeout=DEFAULT_TIMEOUT, session_token=None):
    """调用火山引擎 QueryBalanceAcct 查询资金账户余额。"""
    if _VOLC_IMPORT_ERROR is not None:
        raise BalanceError(
            f"未安装火山引擎 SDK，请执行: pip install volcengine  ({_VOLC_IMPORT_ERROR})"
        )

    credentials = Credentials(access_key, secret_key, VOLC_SERVICE, region, session_token or "")
    service_info = ServiceInfo(
        VOLC_HOST,
        {"Accept": "application/json"},
        credentials,
        timeout,
        timeout,
        "https",
    )
    api_info = {
        "QueryBalanceAcct": ApiInfo(
            method="GET",
            path="/",
            query={},
            form={},
            header={"Accept": "application/json"},
        )
    }

    svc = Service(service_info, api_info)
    svc.set_ak(access_key)
    svc.set_sk(secret_key)
    if session_token:
        svc.set_session_token(session_token)

    params = {"Action": "QueryBalanceAcct", "Version": VOLC_API_VERSION}

    try:
        raw = svc.get("QueryBalanceAcct", params)
    except Exception as exc:
        raise BalanceError(f"火山引擎请求失败: {_sanitize(str(exc), access_key)}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BalanceError(f"火山引擎返回非 JSON 内容: {_sanitize(raw, access_key)[:200]}") from exc

    meta = payload.get("ResponseMetadata", {})
    if "Error" in meta:
        err = meta["Error"]
        raise BalanceError(
            f"火山引擎返回错误 [{err.get('Code')}] "
            f"{_sanitize(str(err.get('Message')), access_key)}"
        )

    result = payload.get("Result") or {}
    if not result:
        raise BalanceError(f"火山引擎返回结果为空: {_sanitize(raw, access_key)[:200]}")

    return {
        "account_id": result.get("AccountID"),
        "available_balance": _to_float(result.get("AvailableBalance")),
        "cash_balance": _to_float(result.get("CashBalance")),
        "freeze_amount": _to_float(result.get("FreezeAmount")),
        "arrears_balance": _to_float(result.get("ArrearsBalance")),
        "credit_limit": _to_float(result.get("CreditLimit")),
        "currency": "CNY",
        "raw": result,
    }


def _render(data):
    """精简为一行：可用余额（耗尽时提示充值）。"""
    avail = _to_float(data.get("available_balance"))
    if avail is None:
        return "火山引擎 可用余额 未知"
    line = f"火山引擎 可用余额 {_fmt(avail, ' 元')}"
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
    access_key = str(a.get("access_key") or os.environ.get("VOLC_ACCESS_KEY", "")).strip()
    secret_key = str(a.get("secret_key") or os.environ.get("VOLC_SECRET_KEY", "")).strip()
    if not access_key or not secret_key:
        return error(
            "缺少火山引擎凭证（VOLC_ACCESS_KEY / VOLC_SECRET_KEY），"
            "请配置同目录 config.json",
            code="not_configured",
        )

    region = (str(a.get("region") or os.environ.get("VOLC_REGION", "")).strip()
              or VOLC_DEFAULT_REGION)
    session_token = str(a.get("session_token") or os.environ.get("VOLC_SESSION_TOKEN", "")).strip() or None

    try:
        data = query_volc(access_key, secret_key, region, timeout, session_token)
    except BalanceError as e:
        return error(str(e), code="query_failed")

    result = {"vendor": "volc", "region": region,
              **{k: v for k, v in data.items() if k != "raw"}}
    if fmt == "json":
        result["raw"] = data.get("raw")
    else:
        result["text"] = _render(data)
    return ok(result)


if __name__ == "__main__":  # 本地调试：python skill.py '{"format": "json"}'
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
