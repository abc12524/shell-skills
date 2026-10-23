# skill: qbittorrent
# description: qBittorrent 下载管理（列表 / 详情 / 新建 / 暂停 / 继续 / 批量删除，可连同文件删除）
# status: enable
# usage:
#   arguments.action = add | list | info | pause | resume | delete（默认 list）
#   list: filter / category / keyword / limit / sort / order / show_completion
#   add: source 或 sources（数组 / 换行分隔）/ save_path / category / paused
#   info / pause / resume: keyword（hash 或名称关键词）
#   delete: keyword | hashes=a|b|c | before=YYYY-MM-DD | after=... | all=true[+filter]
#           可加 delete_files=true、dry_run=true（只列出待删内容不执行）
#   连接: config.json 的 QBIT_*，或 QBIT_API_KEY 走 Bearer 认证
#   示例: {"action":"list","filter":"downloading"}
import base64
import json
import os
import re
import sys
import time
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


_INVALID_TS = (None, 0, -1, 4294967295)
_SORT_KEYS = {
    "added": lambda t: t.get("added_on") or 0,
    "completed": lambda t: t.get("completion_on") or 0,
    "name": lambda t: str(t.get("name") or "").lower(),
    "progress": lambda t: float(t.get("progress") or 0),
    "size": lambda t: t.get("size") or 0,
    "ratio": lambda t: float(t.get("ratio") or 0),
}


def _fmt_ts(ts):
    """qBittorrent 的 unix 时间戳 -> 本地时间字符串；未完成 / 无效返回 None。"""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if ts in _INVALID_TS or ts < 0:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OSError, ValueError, OverflowError):
        return None


def _sort_items(items, sort_by="completed", order="desc"):
    key = _SORT_KEYS.get(sort_by)
    if key is None:
        raise QbitError("不支持的 sort '%s'，可用: %s"
                        % (sort_by, " / ".join(sorted(_SORT_KEYS))), "invalid_argument")
    reverse = str(order or "desc").lower() != "asc"
    return sorted(items, key=key, reverse=reverse)


_BATCH_CHUNK = 100
_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
               "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d")


def _split_tokens(value, pattern=r"[\r\n|,;\s]+"):
    """把 list（可嵌套）/ 分隔字符串统一成去空白、去重的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            for token in _split_tokens(item, pattern):
                if token not in out:
                    out.append(token)
        return out
    out = []
    for token in re.split(pattern, str(value)):
        token = token.strip()
        if token and token not in out:
            out.append(token)
    return out


def _parse_ts_bound(value, label="时间"):
    """支持 unix 时间戳、'YYYY-MM-DD'、'YYYY-MM-DD HH:MM[:SS]'（也接受 '/'）。"""
    text = str(value).strip()
    if not text:
        raise QbitError("%s 不能为空" % label, "invalid_argument")
    if re.fullmatch(r"\d{9,11}", text):
        return int(text)
    for fmt in _TS_FORMATS:
        try:
            return int(time.mktime(time.strptime(text, fmt)))
        except ValueError:
            continue
    raise QbitError("%s 格式无法识别: %s（应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）"
                    % (label, text), "invalid_argument")


def _md_cell(value):
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def _markdown_table(items, show_completion=True):
    if not items:
        return "暂无下载任务"
    cols = ["名称", "状态", "进度", "大小", "下载", "上传", "分享率"]
    aligns = ["---", "---", "---:", "---:", "---:", "---:", "---:"]
    cols += ["ETA", "分类"]
    aligns += ["---:", "---"]
    if show_completion:
        cols.append("完成时间")
        aligns.append("---")
    out = ["| " + " | ".join(cols) + " |", "| " + " | ".join(aligns) + " |"]
    for t in items:
        s = _torrent_summary(t)
        cells = [_md_cell(s["name"]), _md_cell(s["state"]), "%.1f%%" % s["progress"],
                 s["size_human"], _human_speed(s["dlspeed"]), _human_speed(s["upspeed"]),
                 "%.2f" % float(s["ratio"] or 0), _format_eta(s["eta_seconds"]),
                 _md_cell(s["category"])]
        if show_completion:
            cells.append(_md_cell(s["completion_time"] or "-"))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


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
        "added_time": _fmt_ts(t.get("added_on")),
        "completion_on": t.get("completion_on"),
        "completion_time": _fmt_ts(t.get("completion_on")),
        "last_activity": t.get("last_activity"),
        "last_activity_time": _fmt_ts(t.get("last_activity")),
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
        result = self.add_many([source], save_path, category, paused)
        return result["hashes"][0]

    def add_many(self, sources, save_path=None, category=None, paused=False):
        """批量添加：magnet / URL 走 urls（换行分隔），.torrent 走 multipart 多文件。"""
        sources = [str(s).strip() for s in (sources or []) if str(s).strip()]
        if not sources:
            raise QbitError("add 需要 source / sources（magnet / URL / .torrent 路径）",
                            "invalid_argument")
        data = {"paused": "true" if paused else "false"}
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category

        urls, handles, hashes = [], [], []
        try:
            for source in sources:
                if source.startswith(("magnet:", "http://", "https://")):
                    urls.append(source)
                    hashes.append(_magnet_hash(source) if source.startswith("magnet:") else None)
                else:
                    path = os.path.abspath(os.path.expanduser(source))
                    if not os.path.isfile(path):
                        raise QbitError("种子文件不存在: %s" % source, "not_found")
                    fh = open(path, "rb")
                    handles.append(fh)
                    hashes.append(None)
            if urls:
                data["urls"] = "\n".join(urls)
            files = [("torrents", (os.path.basename(fh.name), fh)) for fh in handles] or None
            timeout = 120 if files else 60
            r = self._post("/api/v2/torrents/add", data=data, files=files, timeout=timeout)
        finally:
            for fh in handles:
                try:
                    fh.close()
                except OSError:
                    pass
        text = r.text.strip()
        if text not in ("Ok.", ""):
            raise QbitError("添加失败: %s" % text, "add_failed")
        return {"count": len(sources), "hashes": hashes,
                "urls": len(urls), "files": len(handles)}

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
        self.delete_many([torrent_hash], delete_files)

    def delete_many(self, hashes, delete_files=False):
        """批量删除：hashes 用 | 拼成一次请求，超过 _BATCH_CHUNK 自动分片。"""
        hashes = [str(h).strip() for h in (hashes or []) if str(h).strip()]
        if not hashes:
            raise QbitError("没有要删除的 hash", "invalid_argument")
        for i in range(0, len(hashes), _BATCH_CHUNK):
            chunk = hashes[i:i + _BATCH_CHUNK]
            self._post("/api/v2/torrents/delete",
                       data={"hashes": "|".join(chunk),
                             "deleteFiles": "true" if delete_files else "false"})
        return len(hashes)


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
    sources = _split_tokens(a.get("sources"), r"[\r\n;]+")
    for token in _split_tokens(a.get("source"), r"[\r\n;]+"):
        if token not in sources:
            sources.append(token)
    if not sources:
        raise QbitError("add 需要 source / sources（magnet / URL / .torrent 路径，"
                        "多个用数组或换行分隔）", "invalid_argument")
    save_path = a.get("save_path")
    category = a.get("category")
    paused = _as_bool(a.get("paused"), False)
    result = client.add_many(sources, save_path, category, paused)
    lines = ["| # | 已提交源 | hash |", "| ---: | --- | --- |"]
    for idx, (src, h) in enumerate(zip(sources, result["hashes"]), 1):
        shown = src if len(src) <= 80 else src[:77] + "..."
        lines.append("| %d | %s | %s |" % (idx, _md_cell(shown), h or "-"))
    return ok({"text": "\n".join(lines)})


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
    sort_by = str(a.get("sort") or "completed").strip().lower()
    order = str(a.get("order") or "desc").strip().lower()
    items = _sort_items(items, sort_by, order)
    if limit > 0:
        items = items[:limit]
    show_completion = _as_bool(a.get("show_completion"), True)
    table = _markdown_table(items, show_completion)
    return ok({"action": "list", "filter": filter_by, "category": a.get("category"),
               "sort": sort_by, "order": order, "count": len(items), "text": table})


def _do_info(client, a):
    t = client.find(a.get("keyword"))
    detail = _torrent_summary(t)
    for key in ("downloaded", "uploaded", "num_seeds", "num_leechs", "completed",
                "content_path", "magnet_uri", "comment", "state", "time_active",
                "completion_on", "last_activity"):
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
    delete_files = _as_bool(a.get("delete_files"), False)
    dry_run = _as_bool(a.get("dry_run"), False)
    select_all = _as_bool(a.get("all"), False)
    keyword = str(a.get("keyword") or "").strip()
    hashes_arg = a.get("hashes")
    before = a.get("before")
    after = a.get("after")
    filter_by = str(a.get("filter") or "all").strip().lower()
    if filter_by not in _FILTERS:
        raise QbitError("不支持的 filter '%s'，可用: %s"
                        % (filter_by, " / ".join(sorted(_FILTERS))), "invalid_argument")

    targets = []
    if hashes_arg:
        wanted = _split_tokens(hashes_arg, r"[\r\n|,;\s]+")
        if not wanted:
            raise QbitError("hashes 为空", "invalid_argument")
        pool = client.torrents("all")
        missing = []
        for want in wanted:
            hits = [t for t in pool if str(t.get("hash", "")).lower().startswith(want.lower())]
            if hits:
                targets.extend(hits)
            else:
                missing.append(want)
        if missing:
            raise QbitError("未找到 hash: %s" % ", ".join(missing), "not_found")
    elif keyword and not (select_all or before or after or filter_by != "all"):
        targets = [client.find(keyword)]
    elif keyword or select_all or before or after or filter_by != "all":
        targets = client.torrents(filter_by, a.get("category"))
        if keyword:
            lower = keyword.lower()
            targets = [t for t in targets if lower in str(t.get("name", "")).lower()]
    else:
        raise QbitError("delete 需要筛选条件：keyword / hashes / before / after / "
                        "all=true(+filter, category)", "invalid_argument")

    if before:
        cut = _parse_ts_bound(before, "before")
        targets = [t for t in targets
                   if 0 < int(t.get("completion_on") or 0) < cut]
    if after:
        cut = _parse_ts_bound(after, "after")
        targets = [t for t in targets if int(t.get("completion_on") or 0) >= cut]

    seen, uniq = set(), []
    for t in targets:
        h = t.get("hash")
        if h and h not in seen:
            seen.add(h)
            uniq.append(t)
    targets = sorted(uniq, key=lambda t: (int(t.get("completion_on") or 0),
                                          str(t.get("name") or "")))

    total = sum(int(t.get("size") or 0) for t in targets)
    if not targets:
        return ok({"text": "没有匹配的任务，未执行删除"})
    if dry_run:
        return ok({"text": ("将删除 %d 个任务，合计 %s（试运行，未执行）\n\n"
                            % (len(targets), _human_size(total))) + _markdown_table(targets, True)})
    client.delete_many([t["hash"] for t in targets], delete_files)
    return ok({"text": "已删除 %d 个任务，合计 %s（delete_files=%s）" % (
        len(targets), _human_size(total), "true" if delete_files else "false")})


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
