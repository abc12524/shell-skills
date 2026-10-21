"""多平台余额查询实现：火山引擎 / 智谱 BigModel / DeepSeek。

由原 volc_balance / zhipu_balance 两个技能合并，并新增 DeepSeek。
统一入口 emit()：成功时只返回各平台接口的原始数据，不做二次加工。
"""
import json
import os

import requests

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

# 火山引擎 SDK 为可选依赖，缺失时该平台在 run() 里以可读错误返回（不在导入期退出）
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

ZHIPU_BALANCE_URL = "https://www.bigmodel.cn/api/biz/account/query-customer-account-report"
ZHIPU_QUOTA_URL = "https://www.bigmodel.cn/api/monitor/usage/quota/limit"

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"

DEFAULT_TIMEOUT = 10

VENDORS = ("volc", "zhipu", "deepseek")
VENDOR_LABELS = {
    "volc": "火山引擎",
    "zhipu": "智谱 BigModel",
    "deepseek": "DeepSeek",
}
VENDOR_CONFIG_KEYS = {
    "volc": ("VOLC_ACCESS_KEY", "VOLC_SECRET_KEY"),
    "zhipu": ("ZHIPU_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
}


class BalanceError(Exception):
    """查询失败：message 面向用户，code 为稳定短标识。"""

    def __init__(self, message, code="query_failed"):
        super().__init__(message)
        self.code = code


def _load_config():
    """把本技能文件夹内的 config.json 注入环境变量（不覆盖已有 env）。

    配置与脚本同目录：llm_balance/config.json（模板见 config.example.json），键为环境变量名。
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


def _sanitize(text, *secrets):
    """从错误信息里抹掉密钥，返回的报文常会原样回显 AK。"""
    redacted = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            redacted = redacted.replace(secret, "***")
    return redacted


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


# ---------------------------------------------------------------- 火山引擎

def query_volc(access_key, secret_key, region=VOLC_DEFAULT_REGION,
               timeout=DEFAULT_TIMEOUT, session_token=None):
    """调用火山引擎 QueryBalanceAcct 查询资金账户余额，返回原始 Result。"""
    if _VOLC_IMPORT_ERROR is not None:
        raise BalanceError(
            f"未安装火山引擎 SDK，请执行: pip install volcengine  ({_VOLC_IMPORT_ERROR})",
            code="missing_dependency",
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
        raise BalanceError(
            f"火山引擎请求失败: {_sanitize(exc, access_key, secret_key)}"
        ) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BalanceError(
            f"火山引擎返回非 JSON 内容: {_sanitize(raw, access_key, secret_key)[:200]}"
        ) from exc

    meta = payload.get("ResponseMetadata", {})
    if "Error" in meta:
        err = meta["Error"]
        raise BalanceError(
            f"火山引擎返回错误 [{err.get('Code')}] "
            f"{_sanitize(err.get('Message'), access_key, secret_key)}"
        )

    result = payload.get("Result")
    if not result:
        raise BalanceError(
            f"火山引擎返回结果为空: {_sanitize(raw, access_key, secret_key)[:200]}"
        )
    return result


# ---------------------------------------------------------------- 智谱 BigModel

def _zhipu_headers(api_key, bearer=False):
    token = f"Bearer {api_key}" if bearer else api_key
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "shell-skills/llm_balance",
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
        if code in (401, 403, 1000, 1001, 1002, 1003, 1004):
            raise BalanceError(
                f"鉴权失败 [{code}] {msg}，请检查 ZHIPU_API_KEY", code="auth_failed"
            )

        # 该接口可能返回嵌套的 {code,msg} 错误体（例如 {code:500, msg:{code:50011,...}}），
        # 需要展开取出内层真正的错误码与描述
        inner_code = None
        if isinstance(msg, dict):
            inner_code = msg.get("code")
            msg = msg.get("message") or msg.get("msg") or json.dumps(msg, ensure_ascii=False)

        # 未订阅 Coding Plan 属于正常状态，不是故障
        if str(inner_code) == "50011" or "coding plan" in str(msg).lower():
            raise BalanceError(
                "未订阅 GLM Coding Plan（该账号无此套餐，属正常状态）",
                code="no_coding_plan",
            )

        raise BalanceError(f"智谱返回错误 [{inner_code or code}] {str(msg)[:150]}")

    raise BalanceError(last_error)


def query_zhipu(api_key, timeout=DEFAULT_TIMEOUT):
    """查询智谱账户余额；返回余额与配额接口的原始数据段。"""
    payload = _zhipu_get(ZHIPU_BALANCE_URL, api_key, timeout)
    balance_data = payload.get("data") or payload.get("result") or {}
    if not balance_data:
        raise BalanceError("智谱余额接口返回数据为空")

    quota_data = {}
    try:
        payload = _zhipu_get(ZHIPU_QUOTA_URL, api_key, timeout)
        quota_data = payload.get("data") or payload.get("result") or {}
    except BalanceError as exc:
        if exc.code == "auth_failed":
            raise
        # 未订阅 Coding Plan 等属于正常状态，配额缺失不影响余额结果
        quota_data = {}

    return {"balance": balance_data, "quota": quota_data}


# ---------------------------------------------------------------- DeepSeek

def _deepseek_error(resp):
    """从 DeepSeek 错误响应里取出可读信息。"""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:200] or "未提供错误信息"
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("msg") or json.dumps(err, ensure_ascii=False)
        code = err.get("code") or err.get("type")
        return f"[{code}] {msg}" if code else str(msg)
    if isinstance(err, str):
        return err
    return resp.text[:200] or "未提供错误信息"


def query_deepseek(api_key, timeout=DEFAULT_TIMEOUT):
    """调用 DeepSeek 官方 user/balance 接口，返回原始 payload。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "shell-skills/llm_balance",
    }
    try:
        resp = requests.get(DEEPSEEK_BALANCE_URL, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise BalanceError(f"DeepSeek 网络异常: {exc}", code="network_error") from exc

    if resp.status_code in (401, 403):
        raise BalanceError(
            f"DeepSeek 鉴权失败 [HTTP {resp.status_code}] {_deepseek_error(resp)}，"
            "请检查 DEEPSEEK_API_KEY",
            code="auth_failed",
        )
    if resp.status_code != 200:
        raise BalanceError(
            f"DeepSeek 返回错误 [HTTP {resp.status_code}] {_deepseek_error(resp)}"
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise BalanceError(f"DeepSeek 返回非 JSON 内容: {resp.text[:200]}") from exc


# ---------------------------------------------------------------- 分发

def _query_vendor(vendor, a, timeout):
    """按平台取凭证并查询，返回该平台接口的原始数据。"""
    if vendor == "volc":
        access_key = _first_non_empty(a.get("access_key"), os.environ.get("VOLC_ACCESS_KEY"))
        secret_key = _first_non_empty(a.get("secret_key"), os.environ.get("VOLC_SECRET_KEY"))
        if not access_key or not secret_key:
            raise BalanceError(
                "缺少火山引擎凭证（VOLC_ACCESS_KEY / VOLC_SECRET_KEY），"
                "请配置同目录 config.json",
                code="not_configured",
            )
        region = (_first_non_empty(a.get("region"), os.environ.get("VOLC_REGION"))
                  or VOLC_DEFAULT_REGION)
        session_token = _first_non_empty(
            a.get("session_token"), os.environ.get("VOLC_SESSION_TOKEN")
        ) or None
        return query_volc(access_key, secret_key, region, timeout, session_token)

    env_key = VENDOR_CONFIG_KEYS[vendor][0]
    api_key = _first_non_empty(a.get("api_key"), os.environ.get(env_key))
    if not api_key:
        raise BalanceError(
            f"缺少 {VENDOR_LABELS[vendor]} 凭证（{env_key}），请配置同目录 config.json",
            code="not_configured",
        )
    if vendor == "zhipu":
        return query_zhipu(api_key, timeout)
    return query_deepseek(api_key, timeout)


def dispatch(a):
    """参数校验 + 执行，返回 (ok?, payload)。"""
    try:
        timeout = int(a.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return False, error("arguments.timeout 必须是整数", code="bad_request")

    action = str(a.get("action") or "all").strip().lower()
    if action == "all":
        vendors = list(VENDORS)
    elif action in VENDORS:
        vendors = [action]
    else:
        return False, error(
            f"arguments.action 只能是 all / {' / '.join(VENDORS)}，收到 '{action}'",
            code="unknown_action",
        )

    _load_config()

    results = []
    for vendor in vendors:
        try:
            data = _query_vendor(vendor, a, timeout)
        except BalanceError as exc:
            if action != "all":
                return False, error(str(exc), code=exc.code)
            results.append({
                "vendor": vendor,
                "code": exc.code,
                "error": str(exc),
            })
            continue
        if action != "all":
            return True, ok(data)
        results.append({"vendor": vendor, "raw": data})

    return True, ok({"vendors": results})


def emit(arguments):
    """skill 入口实现：arguments(dict) → ok()/error() 信封。"""
    return dispatch(arguments or {})[1]
