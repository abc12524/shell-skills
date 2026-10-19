# skill: s1
# description: Stage1st（S1）论坛浏览：版块 / 帖子列表 / 帖子内容 / 登录 / 回复
# status: enable
# usage:
#   action = forums（默认）| threads | posts | login | reply
#   forums  无参数
#   threads fid 必填；可选 page / type
#   posts   tid 必填；可选 page / last（读最后 N 页）
#   login   username / password（缺省用 S1_USERNAME / S1_PASSWORD）
#   reply   tid / message（需登录）
#   输出为 Markdown 文本（result.text）
#   示例: {"action": "posts", "tid": "1234567", "last": 3}
#   常用 fid: 归墟/外野=157  动漫论坛=6  ＰＣ数码=51
#   配置: S1_USERNAME / S1_PASSWORD / S1_HOST / S1_COOKIE_FILE；依赖 requests
"""Stage1st（S1）论坛浏览 skill。"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
REQUEST_INTERVAL = 1.0
MAX_FETCH_WORKERS = 4
REQUEST_TIMEOUT = 20
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
        self._rate_lock = threading.Lock()
        self._local = threading.local()
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
        with self._rate_lock:
            elapsed = time.time() - self._last_request
            if elapsed < REQUEST_INTERVAL:
                time.sleep(REQUEST_INTERVAL - elapsed)
            self._last_request = time.time()

    def _get_variables(self, params):
        self._rate_limit()
        resp = self._thread_session().get(f"{self.api_url}/index.php", params=params,
                                          timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return _parse_json(resp.text).get("Variables", {})

    def _thread_session(self):
        """每线程独立 Session（requests.Session 并发不安全），共享同一份 Cookie。"""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self.session.headers)
            session.cookies.update(self.session.cookies)
            self._local.session = session
        return session

    # ── 登录 / 回复 ──
    def login(self, username, password):
        self._rate_limit()
        url = f"{self.api_url}/index.php?module=login&loginsubmit=yes&loginfield=auto&cookietime=2592000"
        resp = self.session.post(url, data={"username": username, "password": password},
                                 timeout=REQUEST_TIMEOUT)
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
            resp = self.session.get(f"{self.api_url}/index.php?module=toplist",
                                    timeout=REQUEST_TIMEOUT)
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
        resp = self.session.post(url, data={"formhash": self._formhash, "tid": tid, "message": message},
                                 timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        body = _parse_json(resp.text)
        if body.get("error"):
            raise S1Error(f"回复失败: {body['error']}")
        return body

    # ── 版块 ──
    def get_forum_groups(self):
        self._rate_limit()
        resp = self.session.get(f"{self.api_url}/index.php?module=forumindex",
                                timeout=REQUEST_TIMEOUT)
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
        variables = self._get_variables(params)
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
    @staticmethod
    def _parse_posts(variables, tid):
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
                datetime=int(p.get("dbdateline") or p.get("dateline") or 0),
                is_op=(p.get("author") == op_author) if op_author else False))
        total_posts = (thread_info.replies + 1) if thread_info else 0
        total_pages = max(1, (total_posts + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
        return posts, total_pages, thread_info

    def _post_params(self, tid, page, author_id=None):
        params = {"module": "viewthread", "version": "4", "ppp": str(POSTS_PER_PAGE),
                  "tid": tid, "page": str(page)}
        if author_id:
            params["authorid"] = author_id
        return params

    def get_posts(self, tid, page=1, author_id=None):
        variables = self._get_variables(self._post_params(tid, page, author_id))
        posts, total_pages, thread_info = self._parse_posts(variables, tid)
        return posts, page, total_pages, thread_info

    def get_last_pages(self, tid, last, author_id=None):
        """并发抓取帖子最后 last 页，返回 (posts, start_page, total_pages, thread_info)。

        第一页用来拿到总页数（Discuz 的回复数在 thread 字段里）；其余页并行请求，
        并发数受 MAX_FETCH_WORKERS 限制，请求间隔仍受 REQUEST_INTERVAL 约束。
        首页已抓到时直接复用，不再重复请求。
        """
        first_posts, _, total_pages, thread_info = self.get_posts(tid, 1, author_id)
        start = max(1, total_pages - last + 1)
        batches = {1: first_posts}
        pages = [p for p in range(start, total_pages + 1) if p != 1]

        if pages:
            def fetch(p):
                variables = self._get_variables(self._post_params(tid, p, author_id))
                return p, self._parse_posts(variables, tid)[0]

            with ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, len(pages))) as pool:
                futures = [pool.submit(fetch, p) for p in pages]
                for future in as_completed(futures):
                    p, batch = future.result()
                    batches[p] = batch

        posts = []
        for p in range(start, total_pages + 1):
            posts.extend(batches.get(p, []))
        return posts, start, total_pages, thread_info


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
def _dedupe_posts(posts):
    """按 pid 去重并保持原顺序（多页抓取时边界楼层可能重复）。"""
    seen = set()
    unique = []
    for p in posts:
        if p.pid and p.pid in seen:
            continue
        if p.pid:
            seen.add(p.pid)
        unique.append(p)
    return unique


def _do_forums(client):
    categories = client.get_forum_groups()
    lines = []
    for c in categories:
        lines += ["=" * 60, f"  {c.name}", "=" * 60]
        for f in c.forums:
            lines.append(f"  [{f.fid:>4}] {f.name}")
            lines.append(f"        threads={f.threads}, today={f.today_posts}")
    return ok({"text": "\n".join(lines)})


def _do_threads(client, a):
    fid = str(a.get("fid") or "").strip()
    if not fid:
        return error("action=threads 需要 fid", code="missing_argument")
    page = _as_int(a.get("page"), 1)
    type_id = a.get("type")
    threads, page, total_pages, thread_types = client.get_threads(fid, page, type_id)
    lines = []
    if thread_types:
        lines.append("类型筛选: " + "  ".join(f"[{t.type_id}] {t.type_name}" for t in thread_types))
    lines.append(f"帖子列表 (第 {page}/{total_pages} 页):")
    for t in threads:
        type_str = f" [{t.type_name}]" if t.type_name else ""
        hide = " [HIDDEN]" if t.is_hide else ""
        lines.append(f"  #{t.tid:<7} {t.title[:50]}{type_str}")
        lines.append(f"          by {t.author}  replies={t.replies}{hide}")
    return ok({"text": "\n".join(lines)})


def _do_posts(client, a):
    tid = str(a.get("tid") or "").strip()
    if not tid:
        return error("action=posts 需要 tid", code="missing_argument")
    page = _as_int(a.get("page"), 1)
    last = _as_int(a.get("last"), 0)

    if last > 0:
        posts, page, total_pages, thread_info = client.get_last_pages(tid, last)
    else:
        posts, page, total_pages, thread_info = client.get_posts(tid, page)
    posts = _dedupe_posts(posts)

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
    return ok({"text": "\n".join(lines)})


def _do_login(client, a):
    username = a.get("username") or os.environ.get("S1_USERNAME")
    password = a.get("password") or os.environ.get("S1_PASSWORD")
    if not username or not password:
        return error("缺少账号/密码：传 username/password，或设置 S1_USERNAME / S1_PASSWORD",
                     code="invalid_argument")
    client.login(username, password)
    return ok({"text": f"已登录: {client.username} (uid={client.uid})"})


def _do_reply(client, a):
    tid = str(a.get("tid") or "").strip()
    message = a.get("message")
    if not tid or not message:
        return error("action=reply 需要 tid 与 message", code="missing_argument")
    username = a.get("username") or os.environ.get("S1_USERNAME")
    password = a.get("password") or os.environ.get("S1_PASSWORD")
    if not client.is_logged_in:
        if not username or not password:
            return error("回复需要登录：传 username/password，或设置 S1_USERNAME / S1_PASSWORD",
                         code="not_configured")
        client.login(username, password)
    client.reply(tid, message)
    return ok({"text": f"已回复 #{tid}"})


def run(arguments):
    """skill 入口：arguments(dict) → ok()/error() 信封。"""
    a = arguments or {}
    _load_config()
    action = str(a.get("action") or "forums").strip().lower()

    try:
        client = S1Client(host=os.environ.get("S1_HOST") or DEFAULT_HOST)
        if action == "forums":
            return _do_forums(client)
        if action == "threads":
            return _do_threads(client, a)
        if action == "posts":
            return _do_posts(client, a)
        if action == "login":
            return _do_login(client, a)
        if action == "reply":
            return _do_reply(client, a)
        return error(f"未知 action '{action}'，可用: forums / threads / posts / login / reply",
                     code="unknown_action")
    except S1Error as e:
        return error(str(e), code="s1_error")
    except requests.exceptions.RequestException as e:
        return error(f"网络请求失败: {e}", code="network_error")


if __name__ == "__main__":
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
