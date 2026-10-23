# skill: home_assistant
# description: Home Assistant 智能家居控制（实体状态 / 服务调用 / 历史 / 模板）
# status: enable
# usage:
#   arguments.action = states | state | call | history | logbook | services | template | config | ping
#     states    列实体，可用 domain / area / search / unavailable 筛选；state 读单个实体
#     call      调服务，需 domain + service + data（如 {"entity_id": "light.x"}）
#     history / logbook  历史与日志，可选 entity_id、hours 或 start/end
#     services  可用服务（给 domain 只看该域，再给 service 看参数）
#     template  渲染 Jinja2，复杂查询首选
#   arguments.entity_id = 实体 ID，逗号分隔可多值（如 light.living_room）
#   arguments.domain    = 域，如 sensor / switch / automation
#   arguments.area      = 区域 ID 或名称；arguments.search = 匹配 entity_id 或友好名
#   arguments.unavailable = true 只看离线/未知实体；detail = true 附带属性和时间戳
#   arguments.limit     = 条数上限（states 100 / logbook 50 / history 0 不限）
#   arguments.hours     = 回看小时数（history 1 / logbook 24），start / end 为 ISO 时间可替代
#   arguments.url / token = 临时覆盖配置里的 HA_URL / HA_TOKEN
#   HTTPS: verify = verify（默认严格）/ none（自签名）；或用 ca_bundle 指定证书文件，
#          或用 fingerprint 固定证书 SHA-256 指纹
#   配置: 同目录 config.json —— HA_URL、HA_TOKEN（可选 HA_VERIFY / HA_CA_BUNDLE / HA_CERT_FINGERPRINT）
#   示例: {"action": "states", "domain": "light"}
#   示例: {"action": "call", "domain": "light", "service": "toggle", "data": {"entity_id": "light.living_room"}}
#   示例: {"action": "template", "template": "{{ states('sensor.temp') }}"}
"""Home Assistant REST API skill（纯标准库，无第三方依赖）。

设计要点：
  * 全部走 /api/ 下的 REST 接口，长期访问令牌 Bearer 鉴权；
  * 输出面向 LLM 消费而精简：默认只给 entity_id / state / name / 单位，
    attributes 与时间戳仅在 detail=true 时返回，避免一次查询灌入几十 KB；
  * 实体/设备/区域注册表只存在于 WebSocket 接口（REST 返回 404），
    因此区域与批量筛选统一用 /api/template 渲染 Jinja2 实现；
  * 只有 call 会改变设备状态，其余动作为只读；
  * 返回统一信封（ok/error），错误带稳定 code；import 期零副作用。
"""
import argparse
import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

DEFAULT_TIMEOUT = 15
DEFAULT_STATES_LIMIT = 100
DEFAULT_LOGBOOK_LIMIT = 50
DEFAULT_HOURS = 1          # history 默认回看 1 小时
DEFAULT_LOGBOOK_HOURS = 24  # logbook 稀疏，默认回看 24 小时才有内容

ACTIONS = (
    "states", "state", "call", "history", "logbook",
    "services", "template", "config", "ping",
)

BAD_STATES = ("unavailable", "unknown")

# 精简输出时保留的常用单位类属性（其余属性丢弃）
KEEP_ATTRS = (
    "unit_of_measurement", "device_class", "state_class",
    "min", "max", "current_temperature", "temperature",
    "brightness", "percentage", "battery_level", "humidity",
)

TIME_KEYS = ("last_changed", "last_updated")


class HaError(Exception):
    """可控错误：message 面向用户，code 为稳定短标识。"""

    def __init__(self, message, code="internal"):
        super().__init__(message)
        self.code = code


def _load_config():
    """把本技能文件夹内的 config.json 注入环境变量（不覆盖已有 env）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(cfg, dict):
        for key, value in cfg.items():
            # 下划线开头的键是模板里的说明性字段（如 _note），不注入环境变量
            if key.startswith("_") or not value or os.environ.get(key):
                continue
            os.environ[key] = str(value)


def _norm_url(url):
    """规范化 base url：去尾部斜杠与误填的 /api 后缀。"""
    url = str(url or "").strip().rstrip("/")
    if url.lower().endswith("/api"):
        url = url[:-4]
    return url


TLS_VERIFY_MODES = ("verify", "none")


def _norm_fingerprint(value):
    """把证书指纹规范成 64 位小写十六进制（兼容冒号分隔与 sha256: 前缀）。"""
    fp = str(value or "").strip().lower()
    if fp.startswith("sha256:"):
        fp = fp[len("sha256:"):]
    return "".join(ch for ch in fp if ch not in ": -").lower()


def _build_ssl_context(verify):
    """按校验模式构建 SSLContext。

    - verify（默认）：用系统 CA 严格校验证书链与主机名，适合公网域名 / 正规 CA；
    - none：跳过证书校验，适合自签名或直接用内网 IP 访问的场景。
      注意：跳过校验不发送密码类凭据仍然安全，但理论上存在中间人风险，
      能拿到证书时建议改用 fingerprint 固定指纹。
    """
    mode = str(verify or "verify").strip().lower()
    if mode not in TLS_VERIFY_MODES:
        raise HaError(
            f"arguments.verify 只能是 {' / '.join(TLS_VERIFY_MODES)}，收到 '{verify}'",
            code="bad_request",
        )
    if mode == "verify":
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

class HaClient:
    """极简 Home Assistant REST 客户端（仅标准库）。"""

    def __init__(self, base_url, token, timeout=DEFAULT_TIMEOUT,
                 verify="verify", fingerprint=None, ca_bundle=None):
        self.base_url = _norm_url(base_url)
        self.token = str(token or "").strip()
        self.timeout = float(timeout)
        self.fingerprint = _norm_fingerprint(fingerprint) or None
        self.ca_bundle = str(ca_bundle).strip() if ca_bundle else None
        self.is_https = self.base_url.lower().startswith("https://")
        self._ctx = None
        if self.is_https:
            if self.ca_bundle:
                # 自建 CA / 指定证书文件：仍然做完整校验，只是换信任锚
                if not os.path.isfile(self.ca_bundle):
                    raise HaError(f"ca_bundle 文件不存在: {self.ca_bundle}", code="bad_request")
                try:
                    self._ctx = ssl.create_default_context(cafile=self.ca_bundle)
                except (ssl.SSLError, ValueError) as exc:
                    raise HaError(f"ca_bundle 证书无法解析: {self.ca_bundle}（{exc}）",
                                  code="bad_request")
            else:
                self._ctx = _build_ssl_context(verify)
            if self.fingerprint and not (
                len(self.fingerprint) == 64
                and all(c in "0123456789abcdef" for c in self.fingerprint)
            ):
                raise HaError(
                    "arguments.fingerprint 应为证书 SHA-256 指纹（64 位十六进制）",
                    code="bad_request",
                )

    def _verify_fingerprint(self, resp):
        """握手后比对服务器证书的 SHA-256 指纹，防止证书被中途替换。"""
        der = b""
        try:
            der = resp.fp.raw._sock.getpeercert(binary_form=True) or b""
        except Exception:
            der = b""
        got = hashlib.sha256(der).hexdigest() if der else ""
        if not got:
            raise HaError(
                "无法获取服务器证书用于指纹校验（可去掉 fingerprint 后重试）",
                code="ssl_error",
            )
        if got != self.fingerprint:
            raise HaError(
                f"证书指纹不匹配，已拒绝连接：期望 {self.fingerprint}，实际 {got}",
                code="ssl_error",
            )

    def _request(self, method, path, payload=None, query=None):
        """发一次请求，返回 (status, body_bytes)。网络层错误直接抛 HaError。"""
        url = f"{self.base_url}/api/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "shell-skills/home_assistant",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        kwargs = {"timeout": self.timeout}
        if self.is_https and self._ctx is not None:
            kwargs["context"] = self._ctx
        try:
            with urllib.request.urlopen(req, **kwargs) as resp:
                if self.is_https and self.fingerprint:
                    self._verify_fingerprint(resp)
                return resp.status, resp.read()
        except ssl.SSLCertVerificationError as e:
            raise HaError(
                f"HTTPS 证书校验失败: {getattr(e, 'verify_message', '') or e}"
                f"（自签名证书可加 verify=\"none\"，或用 ca_bundle 指定证书文件）",
                code="ssl_error",
            )
        except ssl.SSLError as e:
            raise HaError(f"TLS 握手失败: {e}", code="ssl_error")
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            return e.code, body
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise HaError(
                    f"HTTPS 证书校验失败: {getattr(reason, 'verify_message', '') or reason}"
                    f"（自签名证书可加 verify=\"none\"，或用 ca_bundle 指定证书文件）",
                    code="ssl_error",
                )
            if isinstance(reason, ssl.SSLError):
                raise HaError(f"TLS 握手失败: {reason}", code="ssl_error")
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise HaError(f"请求超时（>{self.timeout:g}s）: {self.base_url}", code="timeout")
            raise HaError(f"连不上 {self.base_url}: {reason}", code="network_error")
        except TimeoutError:
            raise HaError(f"请求超时（>{self.timeout:g}s）: {self.base_url}", code="timeout")

    def get(self, path, query=None):
        status, body = self._request("GET", path, query=query)
        return self._handle(status, body, path)

    def post(self, path, payload=None):
        status, body = self._request("POST", path, payload=payload)
        return self._handle(status, body, path)

    def post_text(self, path, payload=None):
        """POST 并返回原始文本。

        /api/template 以 text/plain 返回渲染结果（不是 JSON），
        不能走 _handle 的 json 解析，否则纯文本/数字模板会报 bad_response。
        """
        status, body = self._request("POST", path, payload=payload)
        text = body.decode("utf-8", "replace") if body else ""
        if 200 <= status < 300:
            return text
        if status == 401:
            raise HaError("鉴权失败（401）：HA_TOKEN 无效或已过期", code="auth_failed")
        if status == 400:
            raise HaError(f"模板渲染失败（400）: {text[:300]}", code="bad_request")
        raise HaError(f"HA 返回 HTTP {status}: {text[:300]}", code="http_error")

    @staticmethod
    def _handle(status, body, path):
        text = body.decode("utf-8", "replace") if body else ""
        if 200 <= status < 300:
            if not text.strip():
                return None
            try:
                return json.loads(text)
            except ValueError:
                raise HaError(f"返回非 JSON（{path}）: {text[:200]}", code="bad_response")
        if status == 401:
            raise HaError("鉴权失败（401）：HA_TOKEN 无效或已过期", code="auth_failed")
        if status == 404:
            raise HaError(f"资源不存在（404）: {path}", code="not_found")
        if status == 400:
            raise HaError(f"参数错误（400）: {text[:300]}", code="bad_request")
        raise HaError(f"HA 返回 HTTP {status}: {text[:300]}", code="http_error")


def _split_ids(value):
    """拆分 entity_id：支持逗号分隔字符串与数组。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(_split_ids(v))
        return out
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _parse_time(value, default):
    """解析 ISO 时间；失败抛 HaError。"""
    if value is None or value == "":
        return default
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise HaError(f"时间格式错误: {value!r}（应形如 2026-10-19T00:00:00）", code="bad_request")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _time_range(a, default_hours=DEFAULT_HOURS):
    """由 start/end/hours 计算 (start, end)，end 可能为 None 表示现在。"""
    now = datetime.now(timezone.utc)
    try:
        hours = float(a.get("hours", default_hours))
    except (TypeError, ValueError):
        raise HaError("arguments.hours 必须是数字", code="bad_request")
    if hours <= 0:
        raise HaError("arguments.hours 必须大于 0", code="bad_request")
    start = _parse_time(a.get("start"), now - timedelta(hours=hours))
    end = _parse_time(a.get("end"), None)
    if end is not None and end <= start:
        raise HaError("arguments.end 必须晚于 start", code="bad_request")
    return start.astimezone(timezone.utc), (end.astimezone(timezone.utc) if end else None)


def _fmt_ts(ts):
    """ISO 时间戳 -> 本地可读格式。"""
    if not isinstance(ts, str):
        return ts
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def _to_int(value, default, err):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HaError(err, code="bad_request")


def _to_bool(value):
    """宽松布尔解析：兼容 JSON true/false 与 "true"/"1" 等字符串。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _brief(st, detail=False):
    """精简实体表示：只保留判断一个设备状态真正必需的字段。"""
    attrs = st.get("attributes") or {}
    item = {"entity_id": st.get("entity_id"), "state": st.get("state")}
    name = attrs.get("friendly_name")
    if name:
        item["name"] = name
    unit = attrs.get("unit_of_measurement")
    if unit:
        item["unit"] = unit
    if detail:
        extra = {k: attrs.get(k) for k in KEEP_ATTRS if attrs.get(k) is not None}
        rest = {
            k: v for k, v in attrs.items()
            if k not in KEEP_ATTRS and k != "friendly_name"
            and not isinstance(v, (list, dict))
        }
        if rest:
            extra.update(rest)
        item["attributes"] = extra
        for key in TIME_KEYS:
            if st.get(key):
                item[key] = _fmt_ts(st[key])
    return item


def _render_template(client, template):
    """渲染模板并尽力把 JSON 字面量还原成 Python 对象。"""
    text = client.post_text("template", {"template": template})
    stripped = text.strip()
    if stripped[:1] in ("[", "{"):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


def _area_entity_ids(client, area):
    """用模板把区域（ID 或名称）解析成实体 ID 列表。"""
    want = json.dumps(str(area), ensure_ascii=False)
    tmpl = (
        "{% set want = " + want + " %}"
        "{% set ns = namespace(ids=[]) %}"
        "{% for a in areas() %}"
        "{% if a == want or area_name(a) == want %}"
        "{% set ns.ids = ns.ids + area_entities(a) %}"
        "{% endif %}"
        "{% endfor %}"
        "{{ ns.ids | unique | list | tojson }}"
    )
    ids = _render_template(client, tmpl)
    if not isinstance(ids, list):
        raise HaError(f"区域解析失败: {area}", code="bad_response")
    if not ids:
        raise HaError(f"未找到区域或该区域没有实体: {area}", code="not_found")
    return ids


def _do_states(client, a):
    """列出实体，支持 domain / area / search / unavailable 过滤。"""
    states = client.get("states") or []
    if not isinstance(states, list):
        raise HaError("GET /api/states 返回格式异常", code="bad_response")

    detail = _to_bool(a.get("detail"))
    domain = str(a.get("domain") or "").strip().lower()
    search = str(a.get("search") or "").strip().lower()
    only_bad = _to_bool(a.get("unavailable"))
    limit = _to_int(a.get("limit", DEFAULT_STATES_LIMIT), DEFAULT_STATES_LIMIT,
                    "arguments.limit 必须是整数")

    allowed = None
    area = str(a.get("area") or "").strip()
    if area:
        allowed = set(_area_entity_ids(client, area))

    picked = []
    for st in states:
        if not isinstance(st, dict):
            continue
        eid = str(st.get("entity_id") or "")
        if domain and not eid.lower().startswith(domain + "."):
            continue
        if allowed is not None and eid not in allowed:
            continue
        if only_bad and str(st.get("state")) not in BAD_STATES:
            continue
        if search:
            name = str((st.get("attributes") or {}).get("friendly_name") or "").lower()
            if search not in eid.lower() and search not in name:
                continue
        picked.append(st)

    picked.sort(key=lambda s: str(s.get("entity_id")))
    total = len(picked)
    truncated = limit > 0 and total > limit
    if truncated:
        picked = picked[:limit]

    # 按域汇总，便于一眼看出设备规模
    by_domain = {}
    for st in picked:
        d = str(st.get("entity_id") or "").split(".")[0]
        by_domain[d] = by_domain.get(d, 0) + 1

    result = {"count": total, "returned": len(picked)}
    if truncated:
        result["truncated"] = True
        result["hint"] = f"仅显示前 {limit} 条，可用 domain / area / search 收窄或调大 limit"
    if by_domain:
        result["by_domain"] = by_domain
    result["entities"] = [_brief(s, detail) for s in picked]
    return ok(result)


def _do_state(client, a):
    """读取一个或多个实体的当前状态。"""
    ids = _split_ids(a.get("entity_id"))
    if not ids:
        return error("state 需要 arguments.entity_id", code="missing_argument")
    detail = _to_bool(a.get("detail"))

    found, missing = [], []
    for eid in ids:
        try:
            st = client.get(f"states/{urllib.parse.quote(eid, safe='')}")
        except HaError as exc:
            if exc.code == "not_found":
                missing.append(eid)
                continue
            raise
        if isinstance(st, dict):
            found.append(_brief(st, detail))
        else:
            missing.append(eid)

    if not found:
        return error(f"实体不存在: {', '.join(missing)}", code="not_found")

    if len(found) == 1 and not missing:
        return ok(found[0])
    result = {"count": len(found), "entities": found}
    if missing:
        result["missing"] = missing
    return ok(result)


def _do_history(client, a):
    """查询历史状态，默认只给变化点序列。"""
    ids = _split_ids(a.get("entity_id"))
    if not ids:
        return error("history 需要 arguments.entity_id", code="missing_argument")
    start, end = _time_range(a)
    limit = _to_int(a.get("limit", 0), 0, "arguments.limit 必须是整数")

    query = {"filter_entity_id": ",".join(ids), "minimal_response": "false"}
    if end:
        query["end_time"] = end.isoformat()
    raw = client.get(f"history/period/{urllib.parse.quote(start.isoformat(), safe='')}", query=query)
    if not isinstance(raw, list):
        raise HaError("history 返回格式异常", code="bad_response")

    series = []
    for group in raw:
        if not isinstance(group, list) or not group:
            continue
        eid = group[0].get("entity_id")
        points = []
        for item in group:
            if not isinstance(item, dict) or "state" not in item:
                continue
            points.append({"state": item.get("state"),
                           "at": _fmt_ts(item.get("last_changed") or item.get("last_updated"))})
        if limit > 0 and len(points) > limit:
            points = points[-limit:]
        series.append({"entity_id": eid, "changes": len(points), "history": points})

    return ok({
        "since": start.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "until": (end.astimezone().strftime("%Y-%m-%d %H:%M:%S") if end else "now"),
        "found": len(series),
        "entities": series,
    })


def _do_logbook(client, a):
    """查询日志记录，默认最近 24 小时、最多 50 条。"""
    start, end = _time_range(a, DEFAULT_LOGBOOK_HOURS)
    limit = _to_int(a.get("limit", DEFAULT_LOGBOOK_LIMIT), DEFAULT_LOGBOOK_LIMIT,
                    "arguments.limit 必须是整数")
    query = {}
    ids = _split_ids(a.get("entity_id"))
    if ids:
        query["entity"] = ",".join(ids)
    if end:
        query["end_time"] = end.isoformat()
    raw = client.get(f"logbook/{urllib.parse.quote(start.isoformat(), safe='')}", query=query or None)
    if not isinstance(raw, list):
        raise HaError("logbook 返回格式异常", code="bad_response")

    entries = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        row = {"at": _fmt_ts(e.get("when")),
               "name": e.get("name") or e.get("entity_id"),
               "message": e.get("message")}
        if e.get("state"):
            row["state"] = e.get("state")
        entries.append(row)

    total = len(entries)
    if limit > 0 and total > limit:
        entries = entries[-limit:]
    result = {
        "since": start.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "count": total,
        "returned": len(entries),
        "entries": entries,
    }
    if not total:
        result["hint"] = "该时间段内没有日志记录，可加大 hours 或省略 entity_id"
    return ok(result)


def _do_services(client, a):
    """列出可用服务：无 domain 只给域概览，给 domain 列出该域服务，给 service 给字段说明。"""
    services = client.get("services") or []
    if not isinstance(services, list):
        raise HaError("services 返回格式异常", code="bad_response")

    domain = str(a.get("domain") or "").strip().lower()
    service = str(a.get("service") or "").strip().lower()

    if domain and service:
        for item in services:
            if not isinstance(item, dict) or str(item.get("domain")).lower() != domain:
                continue
            for key, val in (item.get("services") or {}).items():
                if key.lower() != service:
                    continue
                fields = val.get("fields") or {}
                return ok({
                    "domain": domain,
                    "service": key,
                    "description": (val.get("description") or "").strip()[:200] or None,
                    "fields": sorted(fields.keys()),
                    "required": [k for k, v in fields.items() if v.get("required")],
                })
        raise HaError(f"服务不存在: {domain}.{service}", code="not_found")

    rows = []
    for item in services:
        if not isinstance(item, dict):
            continue
        dom = str(item.get("domain") or "")
        if domain and dom.lower() != domain:
            continue
        names = sorted((item.get("services") or {}).keys())
        rows.append({"domain": dom, "services": names})

    rows.sort(key=lambda r: r["domain"])
    if domain:
        if not rows:
            raise HaError(f"域不存在: {domain}", code="not_found")
        return ok({"domain": domain, "services": rows[0]["services"]})

    # 无 domain：只给域与数量，避免一次灌入上百个服务名
    return ok({
        "domains": [{"domain": r["domain"], "count": len(r["services"])} for r in rows],
        "total_domains": len(rows),
        "hint": "给 arguments.domain 查看该域的服务名（如 light）",
    })


def _do_call(client, a):
    """调用服务（唯一会改变设备状态的 action）。"""
    domain = str(a.get("domain") or "").strip()
    service = str(a.get("service") or "").strip()
    if not domain:
        return error("call 需要 arguments.domain（如 switch）", code="missing_argument")
    if not service:
        return error("call 需要 arguments.service（如 turn_on / toggle）", code="missing_argument")

    data = a.get("data")
    if data is None:
        reserved = {"action", "url", "token", "timeout", "domain", "service", "detail"}
        data = {k: v for k, v in a.items() if k not in reserved and v is not None}
    if not isinstance(data, dict):
        return error("arguments.data 必须是对象", code="bad_request")

    ids = _split_ids(data.get("entity_id"))
    if ids:
        data = dict(data, entity_id=ids if len(ids) > 1 else ids[0])
        # HA 对多数 domain 的 turn_on/turn_off 不校验目标实体，写错 ID 也会静默成功，
        # 因此调用前先确认目标存在，避免把拼错实体误报为操作成功。
        known = {str(s.get("entity_id")) for s in (client.get("states") or [])
                 if isinstance(s, dict)}
        unknown = [eid for eid in ids if eid not in known]
        if unknown:
            return error(
                f"实体不存在，未执行调用: {', '.join(unknown)}"
                f"（可用 action=states / search 查找正确 entity_id）",
                code="not_found",
            )

    path = f"services/{urllib.parse.quote(domain, safe='')}/{urllib.parse.quote(service, safe='')}"
    result = client.post(path, data)

    payload = {"ok": True, "domain": domain, "service": service, "target": ids or None}
    if isinstance(result, list) and result:
        payload["changed"] = [
            {"entity_id": s.get("entity_id"), "state": s.get("state")}
            for s in result if isinstance(s, dict)
        ]
    elif result is not None and not isinstance(result, list):
        payload["response"] = result
    return ok(payload)


def _do_template(client, a):
    """渲染 Jinja2 模板。"""
    template = a.get("template")
    if not template or not str(template).strip():
        return error("template 需要 arguments.template", code="missing_argument")
    value = _render_template(client, str(template))
    return ok({"result": value})


def _do_config(client, _a):
    data = client.get("config")
    if not isinstance(data, dict):
        raise HaError("config 返回格式异常", code="bad_response")
    units = data.get("unit_system") or {}
    return ok({
        "version": data.get("version"),
        "location_name": data.get("location_name"),
        "time_zone": data.get("time_zone"),
        "country": data.get("country"),
        "language": data.get("language"),
        "unit_temperature": units.get("temperature") if isinstance(units, dict) else None,
        "state": data.get("state"),
    })


def _do_ping(client, _a):
    """连通性 + 鉴权自检。"""
    status, body = client._request("GET", "")
    text = body.decode("utf-8", "replace").strip()
    if status == 401:
        raise HaError("鉴权失败（401）：HA_TOKEN 无效或已过期", code="auth_failed")
    if not (200 <= status < 300):
        raise HaError(f"HA 返回 HTTP {status}: {text[:200]}", code="http_error")
    api_ok = False
    try:
        api_ok = (json.loads(text) or {}).get("message") == "API running."
    except ValueError:
        api_ok = False
    cfg = client.get("config") or {}
    return ok({
        "ok": api_ok,
        "url": client.base_url,
        "version": cfg.get("version") if isinstance(cfg, dict) else None,
        "location_name": cfg.get("location_name") if isinstance(cfg, dict) else None,
    })


_HANDLERS = {
    "states": _do_states,
    "state": _do_state,
    "call": _do_call,
    "history": _do_history,
    "logbook": _do_logbook,
    "services": _do_services,
    "template": _do_template,
    "config": _do_config,
    "ping": _do_ping,
}


def build_client(a):
    """按 参数 > 环境变量/config.json 的顺序组装客户端。"""
    _load_config()
    url = a.get("url") or os.environ.get("HA_URL", "")
    token = a.get("token") or os.environ.get("HA_TOKEN", "")
    if not url:
        return None, error("缺少 HA 地址（HA_URL），请配置同目录 config.json", code="not_configured")
    if not token:
        return None, error("缺少 HA 令牌（HA_TOKEN），请配置同目录 config.json", code="not_configured")
    try:
        timeout = float(a.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return None, error("arguments.timeout 必须是数字", code="bad_request")

    # HTTPS 相关：verify / fingerprint / ca_bundle 均可来自参数或环境变量
    verify = a.get("verify") or os.environ.get("HA_VERIFY") or "verify"
    fingerprint = a.get("fingerprint") or os.environ.get("HA_CERT_FINGERPRINT")
    ca_bundle = a.get("ca_bundle") or os.environ.get("HA_CA_BUNDLE")
    try:
        return HaClient(url, token, timeout, verify=verify,
                        fingerprint=fingerprint, ca_bundle=ca_bundle), None
    except HaError as exc:
        return None, error(str(exc), code=exc.code)


def run(arguments):
    """skill 入口：arguments(dict) -> ok()/error() 信封。"""
    a = arguments or {}
    action = str(a.get("action") or "").strip().lower()
    if not action:
        return error(f"缺少 action，可选: {' / '.join(ACTIONS)}", code="missing_argument")
    if action not in _HANDLERS:
        return error(f"未知 action '{action}'，可选: {' / '.join(ACTIONS)}", code="unknown_action")

    client, err = build_client(a)
    if err is not None:
        return err

    try:
        return _HANDLERS[action](client, a)
    except HaError as exc:
        return error(str(exc), code=exc.code)
    except Exception as exc:  # 兜底，避免 traceback 抛给 runner
        return error(f"{type(exc).__name__}: {exc}", code="internal")


def _cli():
    p = argparse.ArgumentParser(description="Home Assistant REST API skill（本地调试）")
    p.add_argument("-u", "--url", default=None, help="HA 地址")
    p.add_argument("-t", "--token", default=None, help="长期访问令牌")
    p.add_argument("action", nargs="?", default="ping", choices=ACTIONS)
    p.add_argument("--entity-id", dest="entity_id", default=None)
    p.add_argument("--domain", default=None)
    p.add_argument("--service", default=None)
    p.add_argument("--data", default=None, help="服务参数 JSON")
    p.add_argument("--template", default=None)
    p.add_argument("--area", default=None)
    p.add_argument("--search", default=None)
    p.add_argument("--hours", type=float, default=None)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--verify", default=None, choices=list(TLS_VERIFY_MODES),
                   help="HTTPS 证书校验模式（默认 verify）")
    p.add_argument("--ca-bundle", dest="ca_bundle", default=None, help="指定 CA 证书文件")
    p.add_argument("--fingerprint", default=None, help="证书 SHA-256 指纹（证书固定）")
    p.add_argument("--detail", action="store_true", help="返回完整属性与时间戳")
    p.add_argument("--unavailable", action="store_true", help="只看离线/未知实体")
    args = p.parse_args()

    payload = {"action": args.action}
    for key in ("url", "token", "entity_id", "domain", "service", "template",
                "area", "search", "hours", "start", "end", "limit",
                "verify", "ca_bundle", "fingerprint"):
        val = getattr(args, key)
        if val is not None:
            payload[key] = val
    if args.detail:
        payload["detail"] = True
    if args.unavailable:
        payload["unavailable"] = True
    if args.data:
        try:
            payload["data"] = json.loads(args.data)
        except ValueError:
            p.error("--data 必须是合法 JSON")

    env = run(payload)
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except ValueError:
            print(env)
            return 1
    if isinstance(env, dict) and env.get("status") == "error":
        print(f"[错误] {env.get('error')} (code={env.get('code')})")
        return 1
    print(json.dumps(env, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())