# skill: aria2
# description: aria2 下载管理（新建 / 列表 / 详情 / 暂停 / 继续 / 删除）
# status: enable
# usage:
#   arguments.action = add | list | info | pause | resume | delete（默认 list）
#   add: source(magnet/http/ftp/.torrent) / save_path / out / paused
#   list: filter(all/active/waiting/stopped) / keyword / limit(默认 50)，返回 Markdown 表格
#   其余: keyword(gid 或文件名)；连接见 config.json（ARIA2_RPC_URL / ARIA2_SECRET）
#   示例: {"action":"add","source":"magnet:?xt=urn:btih:..."}
import base64
import json
import os
import re
import sys

try:  # android-agent / kelivo-agent
    from _envelope import ok, error
except ImportError:  # shell-tool
    from core.tools.envelope import ok, error

DEFAULT_TIMEOUT = 30
DEFAULT_LIMIT = 50
_FILTERS = {"all", "active", "waiting", "stopped"}
_FIELDS = ["gid", "status", "totalLength", "completedLength", "downloadSpeed",
           "uploadSpeed", "files", "dir", "errorMessage", "bittorrent",
           "numSeeders", "connections"]


class Aria2Error(Exception):
    def __init__(self, message, code="aria2_error"):
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
    if seconds in (None, -1, 8640000):
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


def _torrent_name(t):
    info = (t.get("bittorrent") or {}).get("info") or {}
    if info.get("name"):
        return info["name"]
    for item in t.get("files") or []:
        path = item.get("path") or ""
        name = re.split(r"[\\/]", path.rstrip("\\/"))[-1]
        if name:
            return name
    return t.get("gid")


def _eta(t):
    try:
        total = int(t.get("totalLength") or 0)
        done = int(t.get("completedLength") or 0)
        speed = int(t.get("downloadSpeed") or 0)
    except (TypeError, ValueError):
        return None
    if total <= done or speed <= 0:
        return None
    return (total - done) // speed


def _markdown_table(items):
    if not items:
        return "暂无下载任务"
    lines = [
        "| 名称 | 状态 | 进度 | 大小 | 下载 | 上传 | ETA | 错误 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for t in items:
        total = int(t.get("totalLength") or 0)
        done = int(t.get("completedLength") or 0)
        progress = round(done * 100.0 / total, 1) if total > 0 else 0.0
        lines.append("| %s | %s | %.1f%% | %s | %s | %s | %s | %s |" % (
            _md_cell(_torrent_name(t)), _md_cell(t.get("status")), progress,
            _human_size(total), _human_speed(t.get("downloadSpeed")),
            _human_speed(t.get("uploadSpeed")), _format_eta(_eta(t)),
            _md_cell(t.get("errorMessage"))))
    return "\n".join(lines)


class Aria2Client:
    def __init__(self, url, secret="", timeout=DEFAULT_TIMEOUT):
        import requests

        self._requests = requests
        self.url = url
        self.secret = secret
        self.timeout = timeout
        self.session = requests.Session()

    def _params(self, params):
        return (["token:" + self.secret] if self.secret else []) + list(params)

    def call(self, method, params):
        payload = {"jsonrpc": "2.0", "id": "skill",
                   "method": "aria2." + method, "params": self._params(params)}
        try:
            r = self.session.post(self.url, json=payload, timeout=self.timeout)
        except self._requests.RequestException as exc:
            raise Aria2Error("无法连接 %s: %s" % (self.url, exc), "connect_failed")
        if r.status_code >= 400:
            raise Aria2Error("HTTP %s: %s" % (r.status_code, r.text.strip()[:200]), "http_error")
        try:
            data = r.json()
        except ValueError:
            raise Aria2Error("响应不是合法 JSON", "bad_response")
        if data.get("error"):
            err = data["error"]
            raise Aria2Error(str(err.get("message") or err), "rpc_error")
        return data.get("result")

    def add(self, source, save_path=None, out=None, paused=False):
        options = {}
        if save_path:
            options["dir"] = save_path
        if out:
            options["out"] = out
        if paused:
            options["pause"] = "true"
        if source.startswith(("magnet:", "http://", "https://", "ftp://")):
            return self.call("addUri", [[source], options])
        path = os.path.abspath(os.path.expanduser(source))
        if not os.path.isfile(path):
            raise Aria2Error("种子文件不存在: %s" % source, "not_found")
        with open(path, "rb") as fh:
            metainfo = base64.b64encode(fh.read()).decode("ascii")
        return self.call("addTorrent", [metainfo, [], options])

    def active(self):
        return self.call("tellActive", [_FIELDS])

    def waiting(self, limit):
        return self.call("tellWaiting", [0, limit, _FIELDS])

    def stopped(self, limit):
        return self.call("tellStopped", [0, limit, _FIELDS])

    def status(self, gid):
        return self.call("tellStatus", [gid, _FIELDS])

    def pause(self, gid):
        self.call("pause", [gid])

    def resume(self, gid):
        self.call("unpause", [gid])

    def delete(self, gid):
        for method in ("remove", "removeDownloadResult"):
            try:
                self.call(method, [gid])
            except Aria2Error:
                pass


def _client(a):
    _load_config()
    url = str(a.get("rpc_url") or os.environ.get("ARIA2_RPC_URL")
              or "http://127.0.0.1:6800/jsonrpc").strip()
    secret = str(a.get("secret") or os.environ.get("ARIA2_SECRET") or "").strip()
    timeout = a.get("timeout") or DEFAULT_TIMEOUT
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise Aria2Error("timeout 必须是数字", "invalid_argument")
    return Aria2Client(url, secret, timeout)


def _all_items(client, limit):
    items = list(client.active())
    items += list(client.waiting(max(limit, 1)))
    items += list(client.stopped(max(limit, 1)))
    return items


def _find(client, keyword):
    keyword = str(keyword or "").strip()
    if not keyword:
        raise Aria2Error("缺少 keyword（gid 或文件名）", "invalid_argument")
    lower = keyword.lower()
    for t in _all_items(client, 1000):
        if t.get("gid", "").lower().startswith(lower) or lower in (_torrent_name(t) or "").lower():
            return t
    raise Aria2Error("未找到匹配的下载: %s" % keyword, "not_found")


def _do_add(client, a):
    source = str(a.get("source") or "").strip()
    if not source:
        raise Aria2Error("add 需要 source（magnet / URL / .torrent 路径）", "invalid_argument")
    gid = client.add(source, a.get("save_path"), a.get("out"),
                     _as_bool(a.get("paused"), False))
    return ok({"action": "add", "gid": gid, "source": source,
               "save_path": a.get("save_path"), "paused": _as_bool(a.get("paused"), False)})


def _do_list(client, a):
    filter_by = str(a.get("filter") or "all").strip().lower()
    if filter_by not in _FILTERS:
        raise Aria2Error("不支持的 filter '%s'，可用: %s" % (filter_by, " / ".join(sorted(_FILTERS))),
                         "invalid_argument")
    try:
        limit = int(a.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raise Aria2Error("limit 必须是数字", "invalid_argument")
    if limit <= 0:
        limit = DEFAULT_LIMIT
    if filter_by == "active":
        items = list(client.active())
    elif filter_by == "waiting":
        items = list(client.waiting(limit))
    elif filter_by == "stopped":
        items = list(client.stopped(limit))
    else:
        items = _all_items(client, limit)
    keyword = str(a.get("keyword") or "").strip().lower()
    if keyword:
        items = [t for t in items if keyword in (_torrent_name(t) or "").lower()]
    items = items[:limit]
    return ok({"action": "list", "filter": filter_by, "count": len(items),
               "text": _markdown_table(items)})


def _do_info(client, a):
    t = _find(client, a.get("keyword"))
    return ok({"action": "info", "torrent": {
        "gid": t.get("gid"),
        "name": _torrent_name(t),
        "status": t.get("status"),
        "size": int(t.get("totalLength") or 0),
        "completed": int(t.get("completedLength") or 0),
        "dlspeed": int(t.get("downloadSpeed") or 0),
        "upspeed": int(t.get("uploadSpeed") or 0),
        "eta_seconds": _eta(t),
        "connections": t.get("connections"),
        "seeders": t.get("numSeeders"),
        "dir": t.get("dir"),
        "error": t.get("errorMessage"),
    }})


def _do_pause(client, a):
    t = _find(client, a.get("keyword"))
    client.pause(t["gid"])
    return ok({"action": "pause", "gid": t["gid"], "name": _torrent_name(t)})


def _do_resume(client, a):
    t = _find(client, a.get("keyword"))
    client.resume(t["gid"])
    return ok({"action": "resume", "gid": t["gid"], "name": _torrent_name(t)})


def _do_delete(client, a):
    t = _find(client, a.get("keyword"))
    client.delete(t["gid"])
    return ok({"action": "delete", "gid": t["gid"], "name": _torrent_name(t),
               "delete_files": False})


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
    except Aria2Error as exc:
        return error(str(exc), code=exc.code)
    except Exception as exc:
        return error("%s: %s" % (type(exc).__name__, exc), code="internal")


if __name__ == "__main__":
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
