# skill: s1
# description: Stage1st（S1 / Discuz!）论坛浏览：版块列表 / 帖子列表 / 帖子内容 / 登录 / 回复
# status: enable
# usage:
#   arguments.cmd = forums | threads | posts | login | reply（默认 forums）
#     forums                      列出全部版块分类与版块
#     threads  arguments.fid      列某版块帖子；可选 arguments.page / arguments.type
#     posts    arguments.tid      看帖子内容；可选 arguments.page / arguments.last
#     login    arguments.username / arguments.password（缺省读 S1_USERNAME / S1_PASSWORD）
#     reply    arguments.tid / arguments.message（需登录，缺省用 S1_USERNAME / S1_PASSWORD 登录）
#   示例: {"cmd": "forums"}
#   示例: {"cmd": "threads", "fid": "4", "page": 1}
#   示例: {"cmd": "posts", "tid": "1234567", "last": 3}
#   示例: {"cmd": "reply", "tid": "1234567", "message": "回复内容"}
#
# 配置：账号/主机读取环境变量（同目录 config.json，模板见 config.example.json）。
#   S1_USERNAME / S1_PASSWORD / S1_HOST / S1_COOKIE_FILE
# 依赖：requests。
"""Stage1st（S1）论坛浏览 skill。"""
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

DEFAULT_HOST = "stage1st.com"
THREADS_PER_PAGE = 50
POSTS_PER_PAGE = 40
REQUEST_INTERVAL = 2.0
DEFAULT_COOKIE_FILE = os.path.join(os.path.expanduser("~"), ".s1_cookies.json")


# ============= 配置 =============
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
            if value and not os.environ.get(key):
                os.environ[key] = str(value)


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class S1Error(Exception):
    """API 返回错误或登录失败。"""


@dataclass
class Forum:
    fid: str
    name: str
    threads: int
    today_posts: int


@dataclass
class ForumCategory:
    name: str
    forums: list


@dataclass
class ThreadType:
    type_id: str
    type_name: str


@dataclass
class Thread:
    tid: str
    title: str
    author: str
    author_id: int
    replies: int
    fid: str
    type_name: Optional[str] = None
    is_hide: bool = False


@dataclass
class Post:
    pid: int
    author: str
    author_id: str
    message: str
    number: str
    is_first: bool
    datetime: int
    is_op: bool = False


def format_timestamp(ts):
    if not ts:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))


class S1Client:
    """S1 Discuz! 论坛客户端（只读操作无需登录；回复需登录）。"""

    def __init__(self, host=DEFAULT_HOST):
        self.base_url = f"https://{host}/2b"
        self.api_url = f"{self.base_url}/api/mobile"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.6099.230 Mobile Safari/537.36 S1Next/200"),
            "Accept": "application/json, text/plain, */*",
        })
        self.cookie_file = os.environ.get("S1_COOKIE_FILE") or DEFAULT_COOKIE_FILE
        self._uid = None
        self._username = None
        self._formhash = None
        self._logged_in = False
        self._last_request = 0.0
        self._load_cookies()

    @property
    def is_logged_in(self):
        return self._logged_in

    @property
    def username(self):
        return self._username

    @property
    def uid(self):
        return self._uid

    def _load_cookies(self):
        if os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, encoding="utf-8") as f:
                    for k, v in json.load(f).items():
                        self.session.cookies[k] = v
            except (json.JSONDecodeError, OSError):
                pass

    def _save_cookies(self):
        try:
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(dict(self.session.cookies), f)
        except OSError:
            pass

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - elapsed)
        self._last_request = time.time()

    # ── 登录 / 回复 ──
    def login(self, username, password):
        self._rate_limit()
        url = f"{self.api_url}/index.php?module=login&loginsubmit=yes&loginfield=auto&cookietime=2592000"
        resp = self.session.post(url, data={"username": username, "password": password})
        resp.raise_for_status()
        body = _parse_json(resp.text)
        if body.get("error"):
            raise S1Error(f"登录失败: {body['error']}")
        variables = body.get("Variables", {})
        self._uid = variables.get("member_uid")
        self._username = variables.get("member_username")
        self._formhash = variables.get("formhash")
        if not self._uid:
            msg = body.get("Message", {}).get("messagestr", "未知错误")
            raise S1Error(f"登录失败: {msg}")
        self._logged_in = True
        self._save_cookies()
        self._refresh_formhash()
        return body

    def _refresh_formhash(self):
        try:
            self._rate_limit()
            resp = self.session.get(f"{self.api_url}/index.php?module=toplist")
            resp.raise_for_status()
            formhash = _parse_json(resp.text).get("Variables", {}).get("formhash")
            if formhash:
                self._formhash = formhash
        except Exception:
            pass

    def reply(self, tid, message):
        if not self._logged_in or not self._formhash:
            raise S1Error("回复需要先登录")
        self._rate_limit()
        url = f"{self.api_url}/index.php?module=sendreply&replysubmit=yes"
        resp = self.session.post(url, data={"formhash": self._formhash, "tid": tid, "message": message})
        resp.raise_for_status()
        body = _parse_json(resp.text)
        if body.get("error"):
            raise S1Error(f"回复失败: {body['error']}")
        return body

    # ── 版块 ──
    def get_forum_groups(self):
        self._rate_limit()
        resp = self.session.get(f"{self.api_url}/index.php?module=forumindex")
        resp.raise_for_status()
        variables = _parse_json(resp.text).get("Variables", {})
        cat_list = variables.get("catlist", []) or []
        forums_by_id = {
            f["fid"]: Forum(fid=f["fid"], name=f.get("name", ""),
                            threads=int(f.get("threads", 0)), today_posts=int(f.get("todayposts", 0)))
            for f in (variables.get("forumlist", []) or [])
        }
        categories = []
        for cat in cat_list:
            cat_forums = [forums_by_id[str(fid)] for fid in (cat.get("forums", []) or [])
                          if str(fid) in forums_by_id]
            categories.append(ForumCategory(name=cat.get("name", ""), forums=cat_forums))
        return categories

    # ── 帖子列表 ──
    def get_threads(self, fid, page=1, type_id=None):
        params = {"module": "forumdisplay", "version": "4", "filter": "typeid",
                  "tpp": str(THREADS_PER_PAGE), "fid": fid, "page": str(page)}
        if type_id:
            params["typeid"] = type_id
        self._rate_limit()
        resp = self.session.get(f"{self.api_url}/index.php", params=params)
        resp.raise_for_status()
        variables = _parse_json(resp.text).get("Variables", {})
        raw_threads = variables.get("forum_threadlist", []) or []

        types_node = variables.get("threadtypes", {}) or {}
        raw_types = types_node.get("types") if isinstance(types_node, dict) else None
        thread_types = [ThreadType(type_id=str(k), type_name=str(v))
                        for k, v in raw_types.items()] if isinstance(raw_types, dict) else []
        type_map = {t.type_id: t.type_name for t in thread_types}

        forum_info = variables.get("forum", {}) or {}
        total_threads = int(forum_info.get("threads", 0)) if isinstance(forum_info, dict) else 0
        total_pages = max(1, (total_threads + THREADS_PER_PAGE - 1) // THREADS_PER_PAGE)

        threads = [Thread(tid=str(t["tid"]), title=t.get("subject", ""), author=t.get("author", ""),
                          author_id=int(t.get("authorId", 0)), replies=int(t.get("replies", 0)),
                          fid=t.get("fid", fid), type_name=type_map.get(t.get("typeid", "")),
                          is_hide=t.get("_isHide", False)) for t in raw_threads]
        return threads, page, total_pages, thread_types

    # ── 帖子内容 ──
    def get_posts(self, tid, page=1, author_id=None):
        params = {"module": "viewthread", "version": "4", "ppp": str(POSTS_PER_PAGE),
                  "tid": tid, "page": str(page)}
        if author_id:
            params["authorid"] = author_id
        self._rate_limit()
        resp = self.session.get(f"{self.api_url}/index.php", params=params)
        resp.raise_for_status()
        variables = _parse_json(resp.text).get("Variables", {})
        raw_posts = variables.get("postlist", []) or []

        info_raw = variables.get("thread")
        thread_info = None
        if isinstance(info_raw, dict):
            thread_info = Thread(tid=str(info_raw.get("tid", tid)), title=info_raw.get("subject", ""),
                                 author=info_raw.get("author", ""), author_id=int(info_raw.get("authorId", 0)),
                                 replies=int(info_raw.get("replies", 0)), fid=info_raw.get("fid", ""))
        op_author = thread_info.author if thread_info else None

        posts = []
        for p in raw_posts:
            message = p.get("message") or p.get("_reply") or ""
            posts.append(Post(
                pid=int(p.get("pid", 0)), author=p.get("author", ""), author_id=str(p.get("authorid", "")),
                message=message, number=str(p.get("number", "")), is_first=p.get("first") == "1",
                datetime=int(p.get("dbdateline", 0)),
                is_op=(p.get("author") == op_author) if op_author else False))
        total_posts = (thread_info.replies + 1) if thread_info else 0
        total_pages = max(1, (total_posts + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
        return posts, page, total_pages, thread_info


def _parse_json(text):
    text = text.strip()
    if text.startswith("//"):
        text = text[2:].strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise


def _strip_html(text):
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(a, b)
    return text.strip()


def _convert_reply_quotes(text):
    def repl(match):
        full = match.group(1)
        am = re.search(r'<font color="#999999">([^<]+) 发表于 ([^<]+)</font>', full)
        author, ts = (am.group(1).strip(), am.group(2).strip()) if am else ("未知", "未知时间")
        cm = re.search(r"</a><br />\r?\n(.*?)$", full, re.DOTALL)
        content = re.sub(r"<[^>]+>", "", cm.group(1).strip()) if cm else ""
        content = content.replace("&amp;", "&")
        quoted = [f"> {line}" for line in content.split("\n") if line.strip()]
        return f"\n[引用] {author} 发表于 {ts}\n" + "\n".join(quoted) + "\n"

    text = re.sub(r'<div class="reply_wrap">(.*?)</div>', repl, text, flags=re.DOTALL)
    return _strip_html(text)


# ============= 命令 =============
def _do_forums(client):
    categories = client.get_forum_groups()
    data = [{"category": c.name,
             "forums": [{"fid": f.fid, "name": f.name, "threads": f.threads,
                         "today_posts": f.today_posts} for f in c.forums]}
            for c in categories]
    lines = []
    for c in categories:
        lines += ["=" * 60, f"  {c.name}", "=" * 60]
        for f in c.forums:
            lines.append(f"  [{f.fid:>4}] {f.name}")
            lines.append(f"        threads={f.threads}, today={f.today_posts}")
    return ok({"categories": data, "text": "\n".join(lines)})


def _do_threads(client, a):
    fid = str(a.get("fid") or "").strip()
    if not fid:
        return error("cmd=threads 需要 fid", code="missing_argument")
    page = _as_int(a.get("page"), 1)
    type_id = a.get("type")
    threads, page, total_pages, thread_types = client.get_threads(fid, page, type_id)
    data = {"page": {"current": page, "total": total_pages},
            "thread_types": [{"type_id": t.type_id, "type_name": t.type_name} for t in thread_types],
            "threads": [{"tid": t.tid, "title": t.title, "author": t.author,
                         "author_id": t.author_id, "replies": t.replies, "type_name": t.type_name}
                        for t in threads]}
    lines = []
    if thread_types:
        lines.append("类型筛选: " + "  ".join(f"[{t.type_id}] {t.type_name}" for t in thread_types))
    lines.append(f"帖子列表 (第 {page}/{total_pages} 页):")
    for t in threads:
        type_str = f" [{t.type_name}]" if t.type_name else ""
        hide = " [HIDDEN]" if t.is_hide else ""
        lines.append(f"  #{t.tid:<7} {t.title[:50]}{type_str}")
        lines.append(f"          by {t.author}  replies={t.replies}{hide}")
    return ok({"fid": fid, "page": page, "total_pages": total_pages,
               "threads": data["threads"], "thread_types": data["thread_types"],
               "text": "\n".join(lines)})


def _do_posts(client, a):
    tid = str(a.get("tid") or "").strip()
    if not tid:
        return error("cmd=posts 需要 tid", code="missing_argument")
    page = _as_int(a.get("page"), 1)
    last = _as_int(a.get("last"), 0)

    if last > 0:
        first_posts, _, total_pages, thread_info = client.get_posts(tid, page=1)
        start = max(1, total_pages - last + 1)
        posts = []
        for p in range(start, total_pages + 1):
            batch, _, _, _ = client.get_posts(tid, page=p)
            posts.extend(batch)
        page = start
    else:
        posts, page, total_pages, thread_info = client.get_posts(tid, page)

    data = {"tid": tid,
            "thread": {"title": thread_info.title, "author": thread_info.author} if thread_info else None,
            "page": {"current": page, "total": total_pages},
            "posts": [{"pid": p.pid, "author": p.author, "number": p.number, "is_first": p.is_first,
                       "is_op": p.is_op, "datetime": format_timestamp(p.datetime),
                       "message": _convert_reply_quotes(p.message)}
                      for p in posts]}

    lines = []
    if thread_info:
        lines.append(f"# {thread_info.title}")
        lines.append(f"tid={tid} | 第 {page}/{total_pages} 页 | {thread_info.replies} 回复")
    for p in posts:
        tags = [t for t, on in (("LZ", p.is_first), ("OP", p.is_op)) if on]
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"\n## #{p.number}{tag_str} {p.author}")
        lines.append(f"pid={p.pid} | {format_timestamp(p.datetime)}")
        content = _convert_reply_quotes(p.message)
        lines.append(content if content else "*(无内容)*")
    lines.append(f"\n--- 第 {page}/{total_pages} 页 ---")
    return ok({"tid": tid, "thread": data["thread"], "page": data["page"],
               "posts": data["posts"], "text": "\n".join(lines)})


def _do_login(client, a):
    username = a.get("username") or os.environ.get("S1_USERNAME")
    password = a.get("password") or os.environ.get("S1_PASSWORD")
    if not username or not password:
        return error("缺少账号/密码：传 username/password，或设置 S1_USERNAME / S1_PASSWORD",
                     code="invalid_argument")
    client.login(username, password)
    return ok({"username": client.username, "uid": client.uid,
               "text": f"✅ 已登录: {client.username} (uid={client.uid})"})


def _do_reply(client, a):
    tid = str(a.get("tid") or "").strip()
    message = a.get("message")
    if not tid or not message:
        return error("cmd=reply 需要 tid 与 message", code="missing_argument")
    username = a.get("username") or os.environ.get("S1_USERNAME")
    password = a.get("password") or os.environ.get("S1_PASSWORD")
    if not client.is_logged_in:
        if not username or not password:
            return error("回复需要登录：传 username/password，或设置 S1_USERNAME / S1_PASSWORD",
                         code="not_configured")
        client.login(username, password)
    client.reply(tid, message)
    return ok({"tid": tid, "text": f"✅ 已回复 #{tid}"})


def run(arguments):
    """skill 入口：arguments(dict) → ok()/error() 信封。"""
    a = arguments or {}
    _load_config()
    cmd = str(a.get("cmd") or "forums").strip().lower()

    try:
        client = S1Client(host=os.environ.get("S1_HOST") or DEFAULT_HOST)
        if cmd == "forums":
            return _do_forums(client)
        if cmd == "threads":
            return _do_threads(client, a)
        if cmd == "posts":
            return _do_posts(client, a)
        if cmd == "login":
            return _do_login(client, a)
        if cmd == "reply":
            return _do_reply(client, a)
        return error(f"未知 cmd '{cmd}'，可用: forums / threads / posts / login / reply",
                     code="unknown_action")
    except S1Error as e:
        return error(str(e), code="s1_error")
    except requests.exceptions.RequestException as e:
        return error(f"网络请求失败: {e}", code="network_error")


if __name__ == "__main__":
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
