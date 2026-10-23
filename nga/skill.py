# skill: nga
# description: NGA 论坛（bbs.nga.cn）浏览：版块分类 / 版块帖子列表 / 帖子回复 / 版块搜索 / 登录
# status: enable
# usage:
#   action = forums（默认）| threads | posts | search | login
#   forums  keyword 可选；按板块名查询，缺省显示分类概览
#   threads fid 必填；可选 page / stid
#   posts   tid 必填；可选 page
#   search  keyword 必填
#   login   cookie（缺省读 NGA_COOKIE）
#   输出为 Markdown 文本（result.text）
#   示例: {"action": "posts", "tid": 46826141}
#   配置: NGA_DOMAIN / NGA_COOKIE / NGA_INSECURE / NGA_COOKIE_FILE；依赖 requests
"""NGA 论坛浏览 skill。"""
import json
import os
import re
import time

import requests

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

FORUMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forums.json")
DEFAULT_DOMAIN = "https://bbs.nga.cn"
DEFAULT_COOKIE_FILE = os.path.join(os.path.expanduser("~"), ".nga_cookies.json")
DOMAINS = [
    "https://bbs.ngacn.cc",
    "https://bbs.nga.cn",
    "https://nga.178.com",
    "https://nga.donews.com",
    "https://ngabbs.com",
]


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


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


# ============= 客户端 =============
class NGAClient:
    def __init__(self, domain=DEFAULT_DOMAIN, cookie_str=None, insecure=False):
        self.domain = domain
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Nga_Official/80023"})
        self.session.verify = not insecure
        self.cookie_file = os.environ.get("NGA_COOKIE_FILE") or DEFAULT_COOKIE_FILE
        if cookie_str:
            self.session.cookies.update(_parse_cookie(cookie_str))
        self._load_cookies()
        self._domains_to_try = [d for d in DOMAINS if d != domain]
        self._last_request_time = 0.0
        self._min_interval = 1.0

    def _enforce_rate_limit(self):
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()

    def _load_cookies(self):
        if os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, encoding="utf-8") as f:
                    self.session.cookies.update(json.load(f))
            except (OSError, ValueError):
                pass

    def save_cookies(self):
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(dict(self.session.cookies), f)
        return self.cookie_file

    def _get(self, url, params=None):
        self._enforce_rate_limit()
        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.encoding = "GBK"
            return resp.text
        except requests.exceptions.SSLError:
            for alt in self._domains_to_try:
                try:
                    resp = self.session.get(url.replace(self.domain, alt, 1), params=params, timeout=30)
                    resp.encoding = "GBK"
                    self.domain = alt
                    return resp.text
                except requests.exceptions.SSLError:
                    continue
            raise

    def get_forum_topics(self, fid, page=1, stid=None):
        params = {"page": page, "lite": "js", "noprefix": ""}
        params["stid" if stid else "fid"] = stid or fid
        return _loads_tolerant(_clean_js(self._get(f"{self.domain}/thread.php", params)))[0]

    def read_topic(self, tid, page=1):
        params = {"tid": tid, "page": page, "__output": "8", "noprefix": "", "v2": ""}
        data, truncated = _loads_tolerant(_clean_js(self._get(f"{self.domain}/read.php", params)))
        if truncated and isinstance(data, dict):
            data["_truncated"] = True
        return data

    def search_forum(self, keyword):
        return _loads_tolerant(_clean_js(self._get(f"{self.domain}/forum.php", {"__output": "8", "key": keyword})))[0]


# ============= 解析助手 =============
def _parse_cookie(cookie_str):
    cookies = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies


def _clean_js(text):
    """去掉 NGA 响应里的 JS 包裹并修复 JSON 边界情况。"""
    if text.startswith("window.script_muti_get_var_store="):
        text = text[len("window.script_muti_get_var_store="):]
    if "/*error fill content" in text:
        text = text[:text.index("/*error fill content")]
    text = re.sub(r'/\*\$\s*js\s*\$\*/', '', text)
    text = re.sub(r'"content":\+(\d+),', r'"content":"+\1",', text)
    text = re.sub(r'"content":(0\d+),', r'"content":"\1",', text)
    text = re.sub(r'"subject":\+(\d+),', r'"subject":"+\1",', text)
    text = re.sub(r'"subject":(0\d+),', r'"subject":"\1",', text)
    text = re.sub(r'"author":(0\d+),', r'"author":"\1",', text)
    text = re.sub(r'"alterinfo":"\[(\w|\s)+\]\s+",', '', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return text.replace('\t', ' ')


def _repair_truncated_json(text):
    """NGA 偶发返回被截断的 JSON：截到最后一个完整容器闭合处并补全括号。"""
    stack = []
    safe_idx = -1
    safe_stack = None
    instr = False
    esc = False
    for i, c in enumerate(text):
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
            continue
        if c == '"':
            instr = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]":
            if stack:
                stack.pop()
                safe_idx = i + 1
                safe_stack = list(stack)
    if safe_idx < 0 or safe_stack is None:
        return None
    prefix = text[:safe_idx].rstrip().rstrip(",")
    return prefix + "".join(reversed(safe_stack))


def _loads_tolerant(text):
    """尽可能解析 NGA 的 JSON：严格 → 宽松 → 修复截断。返回 (data, truncated)。"""
    for kwargs in ({}, {"strict": False}):
        try:
            return json.loads(text, **kwargs), False
        except ValueError:
            pass
    fixed = _repair_truncated_json(text)
    if fixed is not None:
        for kwargs in ({}, {"strict": False}):
            try:
                return json.loads(fixed, **kwargs), True
            except ValueError:
                pass
    raise ValueError("响应无法解析为 JSON")


def _strip_html(text):
    if not isinstance(text, str):
        return str(text)
    return re.sub(r'<[^>]+>', '', text)


def _unescape(text):
    if not isinstance(text, str):
        text = str(text)
    for a, b in (('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&nbsp;', ' '),
                 ('<br/>', '\n'), ('<br>', '\n')):
        text = text.replace(a, b)
    return text


def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (ValueError, TypeError):
        return str(ts)


def _clean_author(author):
    """去掉 'UID:' 前缀，仅当其后是纯数字。"""
    if isinstance(author, str) and author.startswith("UID:") and author[4:].isdigit():
        return author[4:]
    return author


def _clean_subject(subject):
    subject = _strip_html(subject)
    subject = re.sub(r'\[/?[a-z_]+(?::[^\]]*)?\]', '', subject)
    return re.sub(r'\[/?[a-z_]+\]', '', subject)


# ============= 命令 =============
_FORUMS_CACHE = None


def _load_forums():
    global _FORUMS_CACHE
    if _FORUMS_CACHE is not None:
        return _FORUMS_CACHE
    try:
        with open(FORUMS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _FORUMS_CACHE = data if isinstance(data, list) else []
    except (OSError, ValueError):
        _FORUMS_CACHE = []
    return _FORUMS_CACHE


def _forum_tag(forum):
    if forum.get("stid"):
        return f"stid={forum['stid']}"
    return f"fid={forum.get('fid')}"


def _do_forums(client, a):
    forums = _load_forums()
    if not forums:
        return error("版块数据缺失（forums.json）", code="io_error")
    keyword = str(a.get("keyword") or "").strip()
    if not keyword:
        counts = {}
        for forum in forums:
            category = forum.get("category") or "未分类"
            counts[category] = counts.get(category, 0) + 1
        lines = [f"# NGA 版块（共 {len(forums)} 个）", "",
                 "传入 keyword 查询具体版块，如 {\"action\": \"forums\", \"keyword\": \"晴风村\"}", ""]
        for category, count in counts.items():
            lines.append(f"- {category}：{count} 个")
        return ok({"text": "\n".join(lines)})

    lower = keyword.lower()
    exact, prefix, contains = [], [], []
    for forum in forums:
        name = (forum.get("name") or "").lower()
        if name == lower:
            exact.append(forum)
        elif name.startswith(lower):
            prefix.append(forum)
        elif lower in name:
            contains.append(forum)
    matches = exact + prefix + contains
    if not matches:
        return ok({"text": f"未找到匹配「{keyword}」的版块"})

    limit = _as_int(a.get("limit"), 20)
    shown = matches[:limit]
    lines = ["| 版块 | ID | 分类 | 分组 |", "| --- | --- | --- | --- |"]
    for forum in shown:
        lines.append("| %s | %s | %s | %s |" % (
            forum.get("name", ""), _forum_tag(forum),
            forum.get("category", ""), forum.get("group", "")))
    if len(matches) > len(shown):
        lines.append("")
        lines.append(f"（共 {len(matches)} 个匹配，仅显示前 {len(shown)} 个）")
    return ok({"text": "\n".join(lines)})


def _do_threads(client, a):
    fid = a.get("fid")
    if fid in (None, ""):
        return error("action=threads 需要 fid", code="missing_argument")
    page = _as_int(a.get("page"), 1)
    stid = a.get("stid")
    data = client.get_forum_topics(fid, page, stid)
    if "error" in data:
        return error(f"接口错误: {data.get('error', {}).get('0', 'unknown')}", code="nga_error")

    d = data.get("data", {})
    name = (d.get("__F") or {}).get("name", f"fid={fid}")
    topics = d.get("__T") or {}
    total = d.get("__T__ROWS", 0)
    per_page = d.get("__T__ROWS_PAGE", 1) or 1
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1

    items = []
    for i in range(total):
        t = topics.get(str(i))
        if not t:
            continue
        items.append({"tid": t.get("tid", "?"), "author": _clean_author(t.get("author", "?")),
                      "subject": _clean_subject(t.get("subject", "?")),
                      "replies": t.get("replies", 0), "postdate": _fmt_ts(t.get("postdate", 0))})

    lines = [f"## {name} (第 {page}/{total_pages} 页) [{total} 帖]"]
    for idx, t in enumerate(items, 1):
        lines.append(f"\n[{idx}] {t['subject']}")
        lines.append(f"    author={t['author']} replies={t['replies']} date={t['postdate']} tid={t['tid']}")
    lines.append(f"\n--- 第 {page}/{total_pages} 页 ---")
    return ok({"text": "\n".join(lines)})


def _do_posts(client, a):
    tid = a.get("tid")
    if tid in (None, ""):
        return error("action=posts 需要 tid", code="missing_argument")
    page = _as_int(a.get("page"), 1)
    data = client.read_topic(tid, page)
    if "error" in data:
        return error(f"接口错误: {data.get('error', {}).get('0', 'unknown')}", code="nga_error")

    d = data.get("data", {})
    t_info = d.get("__T")
    if isinstance(t_info, dict):
        subject = _strip_html(t_info.get("subject", ""))
        op_name = t_info.get("author") or ""
        op_authorid = str(t_info.get("authorid", ""))
    else:
        subject, op_name, op_authorid = "", "", ""

    user_map = {}
    for uid_str, u_data in (d.get("__U") or {}).items():
        if isinstance(u_data, dict) and "username" in u_data:
            user_map[uid_str] = u_data["username"]
        elif isinstance(u_data, str):
            user_map[uid_str] = u_data
    if op_name and op_authorid:
        user_map[op_authorid] = op_name
    for uid_str in list(user_map):
        user_map[uid_str] = _clean_author(user_map[uid_str])

    replies_raw = d.get("__R") or {}
    present = [k for k in replies_raw if str(k).isdigit()]
    r_rows = d.get("__R__ROWS") or len(present)
    all_rows = d.get("__ROWS") or len(present)
    total_pages = max(1, (all_rows + 19) // 20) if all_rows else 1

    items = []
    for i in range(r_rows):
        r = replies_raw.get(str(i))
        if not r:
            continue
        authorid = str(r.get("authorid", ""))
        content = _strip_html(_unescape(r.get("content", "")))
        content = re.sub(r'\[quote\].*?\[/quote\]', ' [quote] ', content, flags=re.DOTALL)
        content = re.sub(r'\[uid=(\d+)\](.*?)\[/uid\]',
                         lambda m: m.group(2) if m.group(1) == op_authorid else m.group(1), content)
        content = re.sub(r'\[/?\w+(?::[^\]]*)?(?:=[^\]]*)?\]', '', content)
        content = re.sub(r'\[[a-z_/][^\]]*\]', '', content)
        content = re.sub(r'\n{3,}', '\n\n', content.strip())
        items.append({"lou": r.get("lou", "?"), "author": user_map.get(authorid) or r.get("author", "?"),
                      "content": content, "postdate": _fmt_ts(r.get("postdate", 0)), "pid": r.get("pid", "?")})

    lines = [f"# {subject}", f"tid={tid} | 第 {page}/{total_pages} 页 | {all_rows} 回复\n"]
    for r in items:
        lines.append(f"## #{r['lou']} {r['author']}")
        lines.append(f"pid={r['pid']} | {r['postdate']}")
        lines.append(r["content"] if r["content"] else "*(无内容)*")
        lines.append("")
    if data.get("_truncated"):
        lines.append("（注：NGA 返回被截断，仅显示已完整返回的楼层）")
    lines.append(f"--- 第 {page}/{total_pages} 页 ---")
    return ok({"text": "\n".join(lines)})


def _do_search(client, a):
    keyword = str(a.get("keyword") or "").strip()
    if not keyword:
        return error("action=search 需要 keyword", code="missing_argument")
    data = client.search_forum(keyword)
    d = data.get("data", {})
    results = d.get("__T") or {}
    rows = d.get("__T__ROWS", 0)

    if not rows:
        f_data = d.get("__F")
        if f_data and f_data.get("fid") and f_data.get("name"):
            return ok({"text": f"  {f_data['name']:40s} fid={f_data['fid']}"})
        return ok({"text": f"未找到版块: '{keyword}'"})

    forums = [{"fid": r.get("fid", "?"), "name": r.get("name", "?")}
              for r in (results.get(str(i)) for i in range(rows)) if r]
    lines = [f"搜索 '{keyword}' 结果:"]
    lines += [f"  {f['name']:40s} fid={f['fid']}" for f in forums]
    return ok({"text": "\n".join(lines)})


def _do_login(client, a):
    cookie = a.get("cookie") or os.environ.get("NGA_COOKIE")
    if not cookie:
        return error("action=login 需要 cookie（或设置 NGA_COOKIE）", code="invalid_argument")
    client.session.cookies.update(_parse_cookie(cookie))
    path = client.save_cookies()
    return ok({"text": f"✅ 已保存登录 Cookie → {path}"})


def run(arguments):
    """skill 入口：arguments(dict) → ok()/error() 信封。"""
    a = arguments or {}
    _load_config()
    action = str(a.get("action") or "forums").strip().lower()

    domain = os.environ.get("NGA_DOMAIN") or DEFAULT_DOMAIN
    insecure = _truthy(a.get("insecure")) or _truthy(os.environ.get("NGA_INSECURE"))
    client = NGAClient(domain=domain, insecure=insecure)

    try:
        if action == "forums":
            return _do_forums(client, a)
        if action == "threads":
            return _do_threads(client, a)
        if action == "posts":
            return _do_posts(client, a)
        if action == "search":
            return _do_search(client, a)
        if action == "login":
            return _do_login(client, a)
        return error(f"未知 action '{action}'，可用: forums / threads / posts / search / login",
                     code="unknown_action")
    except requests.exceptions.RequestException as e:
        return error(f"网络请求失败: {e}", code="network_error")
    except (ValueError, KeyError) as e:
        return error(f"解析响应失败: {e}", code="parse_error")


if __name__ == "__main__":
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
