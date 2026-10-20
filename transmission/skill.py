# skill: transmission
# description: Transmission 下载管理（新建 / 列表 / 详情 / 暂停 / 继续 / 删除，可连同文件）
# status: enable
# usage:
#   arguments.action = add | list | info | pause | resume | delete（默认 list）
#   add: source(magnet/URL/.torrent) / save_path / paused；list 返回 Markdown 表格
#   list: filter(all/downloading/seeding/stopped) / keyword / limit(默认 50)
#   其余: keyword(id / hash / 名称)；delete 可加 delete_files=true；连接见 config.json（TR_*）
#   示例: {"action":"add","source":"magnet:?xt=urn:btih:..."}
import base64
import json
import os
import sys

try:  # android-agent / kelivo-agent
    from _envelope import ok, error
except ImportError:  # shell-tool
    from core.tools.envelope import ok, error

DEFAULT_TIMEOUT = 30
DEFAULT_LIMIT = 50
_FILTERS = {"all", "downloading", "seeding", "stopped"}
_STATUS = {0: "stopped", 1: "check-wait", 2: "checking", 3: "download-wait",
           4: "downloading", 5: "seed-wait", 6: "seeding"}
_FIELDS = ["id", "hashString", "name", "status", "totalSize", "percentDone",
           "rateDownload", "rateUpload", "eta", "downloadDir", "errorString",
           "uploadRatio", "addedDate", "peersConnected"]


class TrError(Exception):
    def __init__(self, message, code="transmission_error"):
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
    if seconds in (None, -1):
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
        "| 名称 | 状态 | 进度 | 大小 | 下载 | 上传 | ETA | 目录 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for t in items:
        progress = round(float(t.get("percentDone") or 0) * 100, 1)
        lines.append("| %s | %s | %.1f%% | %s | %s | %s | %s | %s |" % (
            _md_cell(t.get("name")), _STATUS.get(t.get("status"), t.get("status")),
            progress, _human_size(t.get("totalSize")),
            _human_speed(t.get("rateDownload")), _human_speed(t.get("rateUpload")),
            _format_eta(t.get("eta")), _md_cell(t.get("downloadDir"))))
    return "\n".join(lines)


class TrClient:
    def __init__(self, url, username="", password="", timeout=DEFAULT_TIMEOUT):
        import requests

        self._requests = requests
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()
        if username or password:
            self.session.auth = (username, password)
        self.session_id = None

    def _post(self, payload):
        headers = {}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        return self.session.post(self.url, json=payload, headers=headers, timeout=self.timeout)

    def call(self, method, arguments=None):
        payload = {"method": method, "arguments": arguments or {}}
        try:
            r = self._post(payload)
            if r.status_code == 409:
                self.session_id = r.headers.get("X-Transmission-Session-Id")
                if not self.session_id:
                    raise TrError("服务端未返回 X-Transmission-Session-Id", "bad_response")
                r = self._post(payload)
        except self._requests.RequestException as exc:
            raise TrError("无法连接 %s: %s" % (self.url, exc), "connect_failed")
        if r.status_code == 401:
            raise TrError("认证失败，请检查用户名 / 密码", "auth_failed")
        if r.status_code >= 400:
            raise TrError("HTTP %s: %s" % (r.status_code, r.text.strip()[:200]), "http_error")
        try:
            data = r.json()
        except ValueError:
            raise TrError("响应不是合法 JSON", "bad_response")
        if data.get("result") != "success":
            raise TrError(str(data.get("result") or "RPC 调用失败"), "rpc_error")
        return data.get("arguments") or {}

    def add(self, source, save_path=None, paused=False):
        args = {"paused": bool(paused)}
        if save_path:
            args["download-dir"] = save_path
        if source.startswith(("magnet:", "http://", "https://")):
            args["filename"] = source
        else:
            path = os.path.abspath(os.path.expanduser(source))
            if not os.path.isfile(path):
                raise TrError("种子文件不存在: %s" % source, "not_found")
            with open(path, "rb") as fh:
                args["metainfo"] = base64.b64encode(fh.read()).decode("ascii")
        result = self.call("torrent-add", args)
        torrent = result.get("torrent-added") or result.get("torrent-duplicate") or {}
        return torrent

    def torrents(self):
        return self.call("torrent-get", {"fields": _FIELDS}).get("torrents") or []

    def find(self, keyword):
        keyword = str(keyword or "").strip()
        if not keyword:
            raise TrError("缺少 keyword（id / hash / 名称）", "invalid_argument")
        lower = keyword.lower()
        for t in self.torrents():
            if str(t.get("id")) == lower or t.get("hashString", "").lower().startswith(lower) \
                    or lower in t.get("name", "").lower():
                return t
        raise TrError("未找到匹配的种子: %s" % keyword, "not_found")

    def pause(self, torrent_id):
        self.call("torrent-stop", {"ids": [torrent_id]})

    def resume(self, torrent_id):
        self.call("torrent-start", {"ids": [torrent_id]})

    def delete(self, torrent_id, delete_files=False):
        self.call("torrent-remove", {"ids": [torrent_id],
                                     "delete-local-data": bool(delete_files)})


def _client(a):
    _load_config()
    url = str(a.get("url") or os.environ.get("TR_URL") or "").strip()
    if not url:
        host = str(a.get("host") or os.environ.get("TR_HOST") or "127.0.0.1").strip()
        port = a.get("port") or os.environ.get("TR_PORT") or 9091
        path = str(os.environ.get("TR_PATH") or "/transmission/rpc").strip()
        if not path.startswith("/"):
            path = "/" + path
        url = "http://%s:%s%s" % (host, port, path)
    username = str(a.get("username") or os.environ.get("TR_USERNAME") or "")
    password = a.get("password")
    if password is None:
        password = os.environ.get("TR_PASSWORD", "")
    timeout = a.get("timeout") or DEFAULT_TIMEOUT
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise TrError("timeout 必须是数字", "invalid_argument")
    return TrClient(url, username, str(password), timeout)


def _do_add(client, a):
    source = str(a.get("source") or "").strip()
    if not source:
        raise TrError("add 需要 source（magnet / URL / .torrent 路径）", "invalid_argument")
    torrent = client.add(source, a.get("save_path"), _as_bool(a.get("paused"), False))
    if not torrent:
        raise TrError("添加失败，服务端未返回任务信息", "add_failed")
    return ok({"action": "add", "id": torrent.get("id"), "hash": torrent.get("hashString"),
               "name": torrent.get("name"), "source": source,
               "save_path": a.get("save_path"), "paused": _as_bool(a.get("paused"), False)})


def _do_list(client, a):
    filter_by = str(a.get("filter") or "all").strip().lower()
    if filter_by not in _FILTERS:
        raise TrError("不支持的 filter '%s'，可用: %s" % (filter_by, " / ".join(sorted(_FILTERS))),
                      "invalid_argument")
    try:
        limit = int(a.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raise TrError("limit 必须是数字", "invalid_argument")
    if limit <= 0:
        limit = DEFAULT_LIMIT
    items = client.torrents()
    if filter_by == "downloading":
        items = [t for t in items if t.get("status") in (3, 4)]
    elif filter_by == "seeding":
        items = [t for t in items if t.get("status") in (5, 6)]
    elif filter_by == "stopped":
        items = [t for t in items if t.get("status") == 0]
    keyword = str(a.get("keyword") or "").strip().lower()
    if keyword:
        items = [t for t in items if keyword in t.get("name", "").lower()]
    items = items[:limit]
    return ok({"action": "list", "filter": filter_by, "count": len(items),
               "text": _markdown_table(items)})


def _do_info(client, a):
    t = client.find(a.get("keyword"))
    return ok({"action": "info", "torrent": {
        "id": t.get("id"),
        "hash": t.get("hashString"),
        "name": t.get("name"),
        "status": _STATUS.get(t.get("status"), t.get("status")),
        "size": t.get("totalSize"),
        "progress": round(float(t.get("percentDone") or 0) * 100, 1),
        "dlspeed": t.get("rateDownload"),
        "upspeed": t.get("rateUpload"),
        "eta_seconds": None if t.get("eta") in (None, -1) else t.get("eta"),
        "ratio": round(float(t.get("uploadRatio") or 0), 2),
        "peers": t.get("peersConnected"),
        "dir": t.get("downloadDir"),
        "error": t.get("errorString"),
    }})


def _do_pause(client, a):
    t = client.find(a.get("keyword"))
    client.pause(t["id"])
    return ok({"action": "pause", "id": t["id"], "name": t.get("name")})


def _do_resume(client, a):
    t = client.find(a.get("keyword"))
    client.resume(t["id"])
    return ok({"action": "resume", "id": t["id"], "name": t.get("name")})


def _do_delete(client, a):
    t = client.find(a.get("keyword"))
    delete_files = _as_bool(a.get("delete_files"), False)
    client.delete(t["id"], delete_files)
    return ok({"action": "delete", "id": t["id"], "name": t.get("name"),
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
    except TrError as exc:
        return error(str(exc), code=exc.code)
    except Exception as exc:
        return error("%s: %s" % (type(exc).__name__, exc), code="internal")


if __name__ == "__main__":
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
