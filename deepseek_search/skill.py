# skill: deepseek_search
# description: DeepSeek 网页搜索 / 抓取（官方自研搜索引擎），返回结构化网页结果
# status: enable
# usage:
#   arguments.action = search（默认，POST /v1/tools/search）| fetch（POST /v1/tools/fetch）
#   search 模式:
#     arguments.query = 搜索词（必填，1~128 字符，支持搜索语法 site:/"..." /lang:/after:/before: 等）
#     arguments.count = 返回条数，默认 5，范围 1~50
#     arguments.need_content = true|false，是否返回网页正文 content，默认 false
#     arguments.ori_question = 触发搜索的用户原始问题（可选，有助理解意图）
#     arguments.localization = 优先返回本地化结果的 IP 地址（可选，默认空）
#   fetch 模式:
#     arguments.url = 要抓取的 HTTP/HTTPS 网页地址（必填）
#   arguments.timeout = 请求超时秒数，默认 30
#   示例: {"action": "search", "query": "DeepSeek V4 模型 发布", "count": 5}
#         {"action": "fetch", "url": "https://example.com/article"}
import json
import os

import requests

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

KEY_ENV = "DEEPSEEK_SEARCH_KEY"
BASE_ENV = "DEEPSEEK_SEARCH_BASE_URL"
DEFAULT_BASE = "https://api.deepseek.com"
TIMEOUT = 30
MAX_COUNT = 50
_TRUTHY = ("1", "true", "yes", "y", "on")


class _MissingKey(ValueError):
    """缺少 DEEPSEEK_API_KEY：单独一类，便于 run() 给出明确的 code。"""


def _as_bool(value, default=False):
    """宽松解析布尔参数：兼容 JSON 布尔与 "true"/"1" 等字符串。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def _as_int(value, default):
    """宽松解析整型参数，非法值回落默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_config():
    """把本技能文件夹内的 config.json 注入环境变量（不覆盖已有 env）。

    配置与脚本同目录：deepseek_search/config.json（模板见 config.example.json）。
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


def _credential():
    _load_config()
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise _MissingKey(f"未配置环境变量 {KEY_ENV}（在 shell-tool 的 .env 或同目录 config.json 里填写）")
    base = os.environ.get(BASE_ENV, "").strip().rstrip("/") or DEFAULT_BASE
    return key, base


def _post(path, payload, timeout=TIMEOUT):
    """统一 POST：校验 HTTP 状态与 OpenAI 兼容 error 结构，返回解析后的 JSON。"""
    key, base = _credential()
    resp = requests.post(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        message = ""
        try:
            err = resp.json().get("error") or {}
            message = err.get("message") or ""
        except ValueError:
            message = (resp.text or "")[:200]
        raise RuntimeError(f"HTTP {resp.status_code}: {message}")
    return resp.json()


def search(query, count=5, need_content=False, ori_question="", localization="", timeout=TIMEOUT):
    """调用 Search 接口，返回规范化结果列表。"""
    payload = {
        "query": query,
        "count": max(1, min(_as_int(count, 5), MAX_COUNT)),
        "need_content": bool(need_content),
    }
    if ori_question:
        payload["ori_question"] = ori_question
    if localization:
        payload["localization"] = localization

    data = _post("/v1/tools/search", payload, timeout)
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", ""),
            "summary": r.get("summary", ""),
            "content": r.get("content", ""),
            "site_name": r.get("site_name"),
            "site_icon": r.get("site_icon"),
            "date": r.get("date"),
            "authority": r.get("authority"),
            "rerank_score": r.get("rerank_score"),
        }
        for r in data.get("results", [])
    ]


def fetch(url, timeout=TIMEOUT):
    """调用 Fetch 接口，抓取单个网页并以 Markdown 文本返回。"""
    data = _post("/v1/tools/fetch", {"url": url}, timeout)
    return {"title": data.get("title", ""), "content": data.get("content", "")}


def run(arguments):
    """skill 入口：arguments(dict) -> ok()/error() 信封。"""
    a = arguments or {}
    action = (a.get("action") or "search").strip().lower()
    timeout = _as_int(a.get("timeout", TIMEOUT), TIMEOUT)

    try:
        if action == "search":
            query = (a.get("query") or "").strip()
            if not query:
                return error("缺少必填参数 query", code="missing_argument")
            results = search(
                query,
                count=a.get("count", 5),
                need_content=_as_bool(a.get("need_content"), False),
                ori_question=(a.get("ori_question") or "").strip(),
                localization=(a.get("localization") or "").strip(),
                timeout=timeout,
            )
            return ok({"query": query, "count": len(results), "results": results})

        if action == "fetch":
            url = (a.get("url") or "").strip()
            if not url:
                return error("缺少必填参数 url", code="missing_argument")
            return ok(fetch(url, timeout=timeout))

        return error(f"未知 action '{action}'，可用: search, fetch", code="unknown_action")
    except _MissingKey as exc:
        return error(str(exc), code="missing_credential")
    except requests.exceptions.JSONDecodeError as exc:
        return error(f"响应不是合法 JSON: {exc}", code="bad_response")
    except requests.RequestException as exc:
        return error(f"请求失败: {exc}", code="http")
    except RuntimeError as exc:
        return error(str(exc), code="api_error")


if __name__ == "__main__":
    import sys

    _args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "action": "search", "query": "DeepSeek V4 模型 发布", "count": 3
    }
    print(run(_args))
