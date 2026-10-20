# skill: qbittorrent
# description: qBittorrent 下载管理（新建 / 列表 / 详情 / 暂停 / 继续 / 删除，可连同文件删除）
# status: enable
# usage:
#   arguments.action = add | list | info | pause | resume | delete（默认 list）
#   add: source(magnet/URL/.torrent) / save_path / category / paused；list 返回 Markdown 表格
#   其余: keyword(hash 或名称关键词)；delete 可加 delete_files=true 连文件删除
#   连接: config.json 的 QBIT_*；有 QBIT_API_KEY（或参数 api_key）时用 Bearer 认证并跳过账号登录
#   示例: {"action":"list","filter":"downloading"}
import base64
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

try:  # android-agent / kelivo-agent
    from _envelope import ok, error
except ImportError:  # shell-tool
    from core.tools.envelope import ok, error

DEFAULT_TIMEOUT = 30
DEFAULT_LIMIT = 50
_FILTERS = {"all", "downloading", "completed", "paused", "active", "inactive",
            "resumed", "stopped", "seeding", "errored"}


class QbitError(Exception):
    def __init__(self, message, code="qbit_error"):
        super().__init__(message)
        self.code = code


def _load_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            os.environ.setdefault(key, str(value))


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _magnet_hash(magnet):
    if not magnet.startswith("magnet:"):
        return None
    query = parse_qs(urlparse(magnet).query)
    for xt in query.get("xt", []):
        if not xt.lower().startswith("urn:btih:"):
            continue
        value = xt.split(":")[-1]
        if len(value) == 32:
            return base64.b32decode(value.upper()).hex()
        return value.lower()
    return None


def _human_size(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return str(int(num)) + "B" if unit == "B" else "%.1f%s" % (num, unit)
        num /= 1024.0
    return str(value)


def _human_speed(value):
    return _human_size(value) + "/s"


def _format_eta(seconds):
    if seconds in (None, 8640000):
        return "-"
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if seconds < 0:
        return "-"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def _md_cell(value):
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def _markdown_table(items):
    if not items:
        return "暂无下载任务"
    lines = [
        "| 名称 | 状态 | 进度 | 大小 | 下载 | 上传 | ETA | 分类 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for t in items:
        s = _torrent_summary(t)
        lines.append("| %s | %s | %.1f%% | %s | %s | %s | %s | %s |" % (
            _md_cell(s["name"]), _md_cell(s["state"]), s["progress"], s["size_human"],
            _human_speed(s["dlspeed"]), _human_speed(s["upspeed"]),
            _format_eta(s["eta_seconds"]), _md_cell(s["category"])))
    return "\n".join(lines)


def _torrent_summary(t):
    eta = t.get("eta")
    return {
        "hash": t.get("hash"),
        "name": t.get("name"),
        "state": t.get("state"),
        "size": t.get("size"),
        "size_human": _human_size(t.get("size")),
        "progress": round(float(t.get("progress") or 0) * 100, 1),
        "dlspeed": t.get("dlspeed"),
        "upspeed": t.get("upspeed"),
        "ratio": round(float(t.get("ratio") or 0), 2),
        "eta_seconds": None if eta in (None, 8640000) else eta,
        "category": t.get("category"),
        "save_path": t.get("save_path"),
        "added_on": t.get("added_on"),
    }


class QbitClient:
    def __init__(self, url, username, password, timeout=DEFAULT_TIMEOUT, api_key=None):
        import requests

        self._requests = requests
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.api_key = api_key
        self.session = requests.Session()
        if api_key:
            self.session.headers["Authorization"] = "Bearer " + api_key

    def login(self):
        if self.api_key:
            try:
                r = self.session.get(self.url + "/api/v2/app/version", timeout=self.timeout)
            except self._requests.RequestException as exc:
                raise QbitError("无法连接 %s: %s" % (self.url, exc), "connect_failed")
            if r.status_code == 403:
                raise QbitError("API Key 无效或权限不足", "auth_failed")
            self._check(r)
            return self
        try:
            r = self.session.post(
                self.url + "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
        except self._requests.RequestException as exc:
            raise QbitError("无法连接 %s: %s" % (self.url, exc), "connect_failed")
        if r.status_code != 200:
            raise QbitError("登录失败: HTTP %s %s" % (r.status_code, r.text.strip()),
                            "auth_failed")
        if r.text.strip() != "Ok.":
            raise QbitError("登录失败，请检查用户名 / 密码", "auth_failed")
        return self

    def _get(self, path, params=None):
        r = self.session.get(self.url + path, params=params, timeout=self.timeout)
        self._check(r)
        return r

    def _post(self, path, data=None, files=None, timeout=None):
        r = self.session.post(self.url + path, data=data, files=files,
                              timeout=timeout or self.timeout)
        self._check(r)
        return r

    @staticmethod
    def _check(r):
        if r.status_code == 403:
            raise QbitError("请求被拒绝（HTTP 403）：请确认 Web UI 地址与 Host 一致（"
                            "局域网访问应使用 IP 而非 127.0.0.1）", "forbidden")
        if r.status_code >= 400:
            raise QbitError("HTTP %s: %s" % (r.status_code, r.text.strip()[:200]),
                            "http_error")

    def add(self, source, save_path=None, category=None, paused=False):
        data = {"paused": "true" if paused else "false"}
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category

        if source.startswith(("magnet:", "http://", "https://")):
            data["urls"] = source
            r = self._post("/api/v2/torrents/add", data=data, timeout=60)
        else:
            path = os.path.abspath(os.path.expanduser(source))
            if not os.path.isfile(path):
                raise QbitError("种子文件不存在: %s" % source, "not_found")
            with open(path, "rb") as fh:
                r = self._post("/api/v2/torrents/add", data=data,
                               files={"torrents": (os.path.basename(path), fh)}, timeout=120)
        if r.text.strip() != "Ok.":
            raise QbitError("添加失败: %s" % r.text.strip(), "add_failed")
        return _magnet_hash(source)

    def torrents(self, filter_by="all", category=None):
        params = {"filter": filter_by}
        if category:
            params["category"] = category
        data = self._get("/api/v2/torrents/info", params=params).json()
        return data if isinstance(data, list) else []

    def find(self, keyword):
        keyword = str(keyword or "").strip()
        if not keyword:
            raise QbitError("缺少 keyword（hash / 名称关键词）", "invalid_argument")
        lower = keyword.lower()
        for t in self.torrents():
            if t.get("hash", "").lower().startswith(lower) or lower in t.get("name", "").lower():
                return t
        raise QbitError("未找到匹配的种子: %s" % keyword, "not_found")

    def pause(self, torrent_hash):
        self._post("/api/v2/torrents/pause", data={"hashes": torrent_hash})

    def resume(self, torrent_hash):
        self._post("/api/v2/torrents/resume", data={"hashes": torrent_hash})

    def delete(self, torrent_hash, delete_files=False):
        self._post("/api/v2/torrents/delete",
                   data={"hashes": torrent_hash,
                         "deleteFiles": "true" if delete_files else "false"})


def _client(a):
    _load_config()
    url = str(a.get("url") or os.environ.get("QBIT_URL") or "").strip()
    if not url:
        host = str(a.get("host") or os.environ.get("QBIT_HOST") or "127.0.0.1").strip()
        port = a.get("port") or os.environ.get("QBIT_PORT") or 8080
        url = "http://%s:%s" % (host, port)
    api_key = str(a.get("api_key") or os.environ.get("QBIT_API_KEY") or "").strip()
    username = str(a.get("username") or os.environ.get("QBIT_USERNAME") or "").strip()
    password = a.get("password")
    if password is None:
        password = os.environ.get("QBIT_PASSWORD", "")
    if not api_key and not username:
        raise QbitError("缺少认证信息：api_key / QBIT_API_KEY 或 username / QBIT_USERNAME",
                        "invalid_argument")
    timeout = a.get("timeout") or DEFAULT_TIMEOUT
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise QbitError("timeout 必须是数字", "invalid_argument")
    return QbitClient(url, username, str(password), timeout, api_key or None).login()


def _do_add(client, a):
    source = str(a.get("source") or "").strip()
    if not source:
        raise QbitError("add 需要 source（magnet / URL / .torrent 路径）", "invalid_argument")
    torrent_hash = client.add(source, a.get("save_path"), a.get("category"),
                              _as_bool(a.get("paused"), False))
    return ok({"action": "add", "hash": torrent_hash, "source": source,
               "save_path": a.get("save_path"), "category": a.get("category"),
               "paused": _as_bool(a.get("paused"), False)})


def _do_list(client, a):
    filter_by = str(a.get("filter") or "all").strip().lower()
    if filter_by not in _FILTERS:
        raise QbitError("不支持的 filter '%s'，可用: %s" % (filter_by, " / ".join(sorted(_FILTERS))),
                        "invalid_argument")
    keyword = str(a.get("keyword") or "").strip().lower()
    items = client.torrents(filter_by, a.get("category"))
    if keyword:
        items = [t for t in items if keyword in t.get("name", "").lower()]
    try:
        limit = int(a.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raise QbitError("limit 必须是数字", "invalid_argument")
    if limit > 0:
        items = items[:limit]
    table = _markdown_table(items)
    return ok({"action": "list", "filter": filter_by, "category": a.get("category"),
               "count": len(items), "text": table})


def _do_info(client, a):
    t = client.find(a.get("keyword"))
    detail = _torrent_summary(t)
    for key in ("downloaded", "uploaded", "num_seeds", "num_leechs", "completed",
                "content_path", "magnet_uri", "comment", "state", "time_active"):
        if key in t:
            detail[key] = t[key]
    return ok({"action": "info", "torrent": detail})


def _do_pause(client, a):
    t = client.find(a.get("keyword"))
    client.pause(t["hash"])
    return ok({"action": "pause", "hash": t["hash"], "name": t.get("name")})


def _do_resume(client, a):
    t = client.find(a.get("keyword"))
    client.resume(t["hash"])
    return ok({"action": "resume", "hash": t["hash"], "name": t.get("name")})


def _do_delete(client, a):
    t = client.find(a.get("keyword"))
    delete_files = _as_bool(a.get("delete_files"), False)
    client.delete(t["hash"], delete_files)
    return ok({"action": "delete", "hash": t["hash"], "name": t.get("name"),
               "delete_files": delete_files})


_ACTIONS = {
    "add": _do_add,
    "list": _do_list,
    "info": _do_info,
    "pause": _do_pause,
    "resume": _do_resume,
    "delete": _do_delete,
}


def run(arguments):
    a = arguments or {}
    action = str(a.get("action") or "list").strip().lower()
    if action not in _ACTIONS:
        return error("未知 action '%s'，可用: %s" % (action, " / ".join(_ACTIONS)),
                     code="unknown_action")
    try:
        return _ACTIONS[action](_client(a), a)
    except QbitError as exc:
        return error(str(exc), code=exc.code)
    except Exception as exc:
        return error("%s: %s" % (type(exc).__name__, exc), code="internal")


if __name__ == "__main__":
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
