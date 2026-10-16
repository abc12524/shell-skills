# skill: news
# description: 查询 MySQL 新闻库：早报/晚报/热榜（今日）与关键词检索
# status: enable
# usage:
#   arguments.action = today | search
#     today   今日新闻（默认）；arguments.type = hot(默认) | morning | evening
#     search  标题+摘要关键词检索；arguments.keyword = 关键词（必填）
#   arguments.limit  = 条数上限（today 默认 0=全部；search 默认 20）
#   arguments.format = text | markdown | json（默认 text）
#   可选展示参数：url / compact / max_summary
#   示例: {"action": "today", "type": "hot"}
#   示例: {"action": "today", "type": "morning", "format": "markdown"}
#   示例: {"action": "search", "keyword": "国庆", "limit": 5}
#
# 配置：MySQL 连接读取环境变量 MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB/MYSQL_PORT。
#   - shell-tool：由项目根 .env 提供（导入 core.config 时注入 os.environ）。
#   - android-agent / kelivo-agent：由本技能同目录 config.json 提供（模板见 config.example.json）。
# 依赖：pymysql（移动端需自行安装）。
"""查询 MySQL 新闻库的 skill（早报/晚报/热榜 + 关键词检索）。"""
import json
import os
from datetime import date, datetime

import pymysql

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

ACTIONS = ("today", "latest", "range", "search", "stats", "dates")
TYPES = ("hot", "morning", "evening")
TYPE_LABELS = {"hot": "🔥 热点榜", "morning": "🌅 早报", "evening": "🌙 晚报"}
FORMATS = ("text", "markdown", "json")
GROUP_ORDER = ("morning", "hot", "evening")
DEFAULT_LATEST_LIMIT = 10
DEFAULT_SEARCH_LIMIT = 20
DEFAULT_DAYS = 10
DEFAULT_MAX_SUMMARY = 120
TITLE_MAX = 65
LINE = 70

_SELECT_COLS = (
    "id, title, summary, news_type, source_name, source_url, "
    "rank_position, publish_time, fetch_time"
)


# ============= 配置 =============
def _load_config():
    """把本技能文件夹内的 config.json 注入环境变量（不覆盖已有 env）。

    配置与脚本同目录：news/config.json（模板见 config.example.json），键为环境变量名。
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


def _db_config():
    """组装 MySQL 连接参数：config.json/env 优先，shell-tool 下回退 core.config（.env）。"""
    _load_config()
    try:  # shell-tool：导入 core.config 即加载 .env 并注入 os.environ
        import core.config  # noqa: F401
    except ImportError:
        pass
    return {
        "host": os.environ.get("MYSQL_HOST", ""),
        "user": os.environ.get("MYSQL_USER", ""),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DB", ""),
        "port": int(os.environ.get("MYSQL_PORT", "3306") or "3306"),
        "charset": "utf8mb4",
    }


def _db_configured(cfg):
    """host/user/database 任一为空视为未配置。"""
    return bool(cfg["host"] and cfg["user"] and cfg["database"])


# ============= 参数解析 =============
def _truthy(value):
    """宽松解析布尔：兼容 JSON 布尔与 'true'/'1' 等字符串。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def _as_int(value, default):
    """宽松解析整型，非法值回落默认。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_type(a):
    """解析 news_type：返回 hot/morning/evening/all，非法返回 None。"""
    raw = a.get("type", a.get("news_type", "hot"))
    t = str(raw or "hot").strip().lower()
    if t in ("all", "*", ""):
        return "all"
    return t if t in TYPES else None


# ============= 数据访问 =============
def _conn():
    cfg = _db_config()
    cfg["cursorclass"] = pymysql.cursors.DictCursor
    cfg["autocommit"] = True
    return pymysql.connect(**cfg)


def _query(sql, params=()):
    """执行只读查询；有参时用占位符，无参时不触发 pymysql 的 % 格式化。"""
    with _conn() as conn:
        with conn.cursor() as cur:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return cur.fetchall()


def _fmt_dt(value):
    """datetime/date → 'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DD'。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value) if value else ""


def _norm(rows, max_summary):
    """规整查询行：时间格式化 + 摘要按需截断。"""
    items = []
    for r in rows:
        summary = (r.get("summary") or "").strip()
        if max_summary and len(summary) > max_summary:
            summary = summary[:max_summary] + "..."
        items.append({
            "id": r.get("id"),
            "title": (r.get("title") or "").strip(),
            "summary": summary,
            "news_type": r.get("news_type") or "",
            "source_name": r.get("source_name") or "",
            "source_url": r.get("source_url") or "",
            "rank_position": r.get("rank_position"),
            "publish_time": _fmt_dt(r.get("publish_time")),
            "fetch_time": _fmt_dt(r.get("fetch_time")),
        })
    return items


def _fetch_today(news_type, limit):
    if news_type != "all":
        sql = (f"SELECT {_SELECT_COLS} FROM news "
               "WHERE DATE(fetch_time) = CURDATE() AND news_type = %s "
               "ORDER BY rank_position ASC, publish_time DESC")
        rows = _query(sql, (news_type,))
    else:
        sql = (f"SELECT {_SELECT_COLS} FROM news "
               "WHERE DATE(fetch_time) = CURDATE() "
               "ORDER BY news_type, rank_position ASC, publish_time DESC")
        rows = _query(sql)
    return rows[:limit] if limit > 0 else rows


def _fetch_latest(news_type, limit):
    if news_type != "all":
        sql = (f"SELECT {_SELECT_COLS} FROM news WHERE news_type = %s "
               "ORDER BY fetch_time DESC, rank_position ASC LIMIT %s")
        return _query(sql, (news_type, limit))
    sql = (f"SELECT {_SELECT_COLS} FROM news "
           "ORDER BY fetch_time DESC, rank_position ASC LIMIT %s")
    return _query(sql, (limit,))


def _fetch_range(start, end, news_type):
    if news_type != "all":
        sql = (f"SELECT {_SELECT_COLS} FROM news "
               "WHERE DATE(fetch_time) BETWEEN %s AND %s AND news_type = %s "
               "ORDER BY fetch_time DESC, rank_position ASC")
        return _query(sql, (start, end, news_type))
    sql = (f"SELECT {_SELECT_COLS} FROM news "
           "WHERE DATE(fetch_time) BETWEEN %s AND %s "
           "ORDER BY fetch_time DESC, news_type, rank_position ASC")
    return _query(sql, (start, end))


def _fetch_search(keyword, news_type, limit):
    like = f"%{keyword}%"
    if news_type != "all":
        sql = (f"SELECT {_SELECT_COLS} FROM news "
               "WHERE (title LIKE %s OR summary LIKE %s) AND news_type = %s "
               "ORDER BY fetch_time DESC, rank_position ASC LIMIT %s")
        return _query(sql, (like, like, news_type, limit))
    sql = (f"SELECT {_SELECT_COLS} FROM news "
           "WHERE (title LIKE %s OR summary LIKE %s) "
           "ORDER BY fetch_time DESC, rank_position ASC LIMIT %s")
    return _query(sql, (like, like, limit))


def _fetch_stats():
    today_rows = _query(
        "SELECT news_type, COUNT(*) AS count, MIN(fetch_time) AS first_fetch, "
        "MAX(fetch_time) AS last_fetch FROM news WHERE DATE(fetch_time) = CURDATE() "
        "GROUP BY news_type ORDER BY news_type")
    total_rows = _query(
        "SELECT news_type, COUNT(*) AS total_count, MIN(DATE(fetch_time)) AS first_date, "
        "MAX(DATE(fetch_time)) AS last_date FROM news "
        "GROUP BY news_type ORDER BY news_type")
    return [
        {"news_type": r["news_type"], "count": r["count"],
         "first_fetch": _fmt_dt(r["first_fetch"]), "last_fetch": _fmt_dt(r["last_fetch"])}
        for r in today_rows
    ], [
        {"news_type": r["news_type"], "total_count": r["total_count"],
         "first_date": _fmt_dt(r["first_date"]), "last_date": _fmt_dt(r["last_date"])}
        for r in total_rows
    ]


def _fetch_dates(days):
    rows = _query(
        "SELECT DATE(fetch_time) AS d, news_type, COUNT(*) AS count FROM news "
        "WHERE fetch_time >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
        "GROUP BY d, news_type ORDER BY d DESC, news_type", (days,))
    grouped = {}
    for r in rows:
        key = _fmt_dt(r["d"])
        grouped.setdefault(key, {})[r["news_type"]] = r["count"]
    return grouped


# ============= 渲染 =============
def _meta_line(n, show_rank=True):
    parts = [p for p in (n.get("source_name"), n.get("publish_time")) if p]
    if show_rank and n.get("rank_position"):
        parts.append(f"#{n['rank_position']}")
    return " · ".join(parts)


def _render_text(items, heading, news_type, show_url, compact):
    if not items:
        return "暂无新闻数据"
    lines = ["=" * LINE, f"  {heading}", "=" * LINE, ""]

    def block(rows):
        for i, n in enumerate(rows, 1):
            title = n["title"]
            if len(title) > TITLE_MAX:
                title = title[:TITLE_MAX] + "..."
            lines.append(f"{i:2d}. {title}")
            meta = _meta_line(n)
            if meta:
                lines.append(f"    {meta}")
            if not compact and n.get("summary"):
                lines.append(f"    {n['summary']}")
            if show_url and n.get("source_url"):
                lines.append(f"    🔗 {n['source_url']}")
            lines.append("")

    if news_type != "all":
        block(items)
    else:
        grouped = {}
        for n in items:
            grouped.setdefault(n["news_type"], []).append(n)
        for t in GROUP_ORDER:
            if t in grouped:
                lines += ["-" * LINE,
                          f"  {TYPE_LABELS.get(t, t)}  ({len(grouped[t])}条)",
                          "-" * LINE, ""]
                block(grouped[t])
    return "\n".join(lines)


def _render_markdown(items, heading, news_type, show_url):
    if not items:
        return "暂无新闻数据"
    lines = [f"## {heading}", ""]

    def block(rows):
        for i, n in enumerate(rows, 1):
            title = n["title"]
            if show_url and n.get("source_url"):
                lines.append(f"### {i}. [{title}]({n['source_url']})")
            else:
                lines.append(f"### {i}. {title}")
            if n.get("summary"):
                lines.append(f"> {n['summary']}")
            meta = []
            if n.get("source_name"):
                meta.append(f"**来源**: {n['source_name']}")
            if n.get("publish_time"):
                meta.append(f"**时间**: {n['publish_time']}")
            if meta:
                lines.append(" · ".join(meta))
            lines.append("")

    if news_type != "all":
        block(items)
    else:
        grouped = {}
        for n in items:
            grouped.setdefault(n["news_type"], []).append(n)
        for t in GROUP_ORDER:
            if t in grouped:
                lines.append(f"### {TYPE_LABELS.get(t, t)} ({len(grouped[t])}条)")
                lines.append("")
                for n in grouped[t]:
                    title = n["title"]
                    if show_url and n.get("source_url"):
                        lines.append(f"- [{title}]({n['source_url']})")
                    else:
                        lines.append(f"- {title}")
                lines.append("")
    return "\n".join(lines)


def _render_stats(today_rows, total_rows):
    lines = ["=" * 60, f"  【今日新闻抓取统计】 {date.today().strftime('%Y-%m-%d')}",
             "=" * 60, ""]
    if not today_rows:
        lines += ["  ⚠️ 今日暂无抓取记录", ""]
    else:
        total = 0
        for s in today_rows:
            total += s["count"]
            lines += [f"  {TYPE_LABELS.get(s['news_type'], s['news_type'])}",
                      f"    今日抓取: {s['count']} 条",
                      f"    抓取时间: {s['first_fetch']} ~ {s['last_fetch']}", ""]
        lines += [f"  📊 今日总计: {total} 条新闻", ""]
    lines += ["=" * 60, "  【历史统计】", "=" * 60, ""]
    if not total_rows:
        lines += ["  暂无历史数据", ""]
    for s in total_rows:
        lines += [f"  {TYPE_LABELS.get(s['news_type'], s['news_type'])}",
                  f"    总数量: {s['total_count']} 条",
                  f"    时间范围: {s['first_date']} ~ {s['last_date']}", ""]
    lines.append("=" * 60)
    return "\n".join(lines)


def _render_dates(grouped, days):
    if not grouped:
        return f"最近 {days} 天暂无数据"
    lines = [f"最近 {days} 天可用日期：", ""]
    for d in sorted(grouped, reverse=True):
        parts = [f"{TYPE_LABELS.get(t, t)} {grouped[d][t]}条"
                 for t in GROUP_ORDER if t in grouped[d]]
        lines.append(f"  {d}  " + " | ".join(parts))
    return "\n".join(lines)


# ============= 入口 =============
def run(arguments):
    """skill 入口：arguments(dict) → ok()/error() 信封字符串。"""
    a = arguments or {}

    action = str(a.get("action") or "today").strip().lower()
    if action not in ACTIONS:
        return error(f"未知 action '{action}'，可用: {', '.join(ACTIONS)}",
                     code="unknown_action")

    fmt = str(a.get("format") or "text").strip().lower()
    if fmt not in FORMATS:
        return error(f"未知 format '{fmt}'，可用: {', '.join(FORMATS)}",
                     code="bad_request")

    news_type = _parse_type(a)
    if news_type is None:
        return error(f"type 必须是 {' / '.join(TYPES)} / all", code="bad_request")

    if not _db_configured(_db_config()):
        return error("未配置 MySQL 连接（请配置 MYSQL_HOST/MYSQL_USER/MYSQL_DB，"
                     "shell-tool 见 .env、移动端见 config.json）",
                     code="not_configured")

    show_url = _truthy(a.get("url"))
    compact = _truthy(a.get("compact"))
    max_summary = _as_int(a.get("max_summary"), DEFAULT_MAX_SUMMARY)
    if max_summary < 0:
        max_summary = DEFAULT_MAX_SUMMARY
    today_str = date.today().strftime("%Y-%m-%d")

    def render(items, heading, result):
        if fmt == "text":
            result["output"] = _render_text(items, heading, news_type, show_url, compact)
        elif fmt == "markdown":
            result["output"] = _render_markdown(items, heading, news_type, show_url)
        return ok(result)

    try:
        if action == "stats":
            today_rows, total_rows = _fetch_stats()
            result = {"action": action, "today": today_rows, "history": total_rows}
            if fmt in ("text", "markdown"):
                result["output"] = _render_stats(today_rows, total_rows)
            return ok(result)

        if action == "dates":
            days = _as_int(a.get("days"), DEFAULT_DAYS)
            if days <= 0:
                days = DEFAULT_DAYS
            grouped = _fetch_dates(days)
            result = {"action": action, "days": days, "dates": grouped}
            if fmt in ("text", "markdown"):
                result["output"] = _render_dates(grouped, days)
            return ok(result)

        if action == "range":
            start = str(a.get("start") or "").strip()
            end = str(a.get("end") or "").strip()
            if not start or not end:
                return error("action=range 需要 start 与 end（YYYY-MM-DD）",
                             code="missing_argument")
            try:
                if date.fromisoformat(start) > date.fromisoformat(end):
                    return error("start 不能晚于 end", code="bad_request")
            except ValueError:
                return error("日期格式应为 YYYY-MM-DD", code="bad_request")
            limit = _as_int(a.get("limit"), 0)
            rows = _fetch_range(start, end, news_type)
            if limit > 0:
                rows = rows[:limit]
            items = _norm(rows, max_summary)
            heading = (f"{TYPE_LABELS.get(news_type, '📰 全部类型')}  {start} ~ {end}"
                       if news_type != "all" else f"📰 新闻汇总  {start} ~ {end}")
            return render(items, heading,
                          {"action": action, "type": news_type, "start": start,
                           "end": end, "count": len(items), "items": items})

        if action == "search":
            keyword = str(a.get("keyword") or "").strip()
            if not keyword:
                return error("action=search 需要 keyword", code="missing_argument")
            limit = _as_int(a.get("limit"), DEFAULT_SEARCH_LIMIT)
            if limit <= 0:
                limit = DEFAULT_SEARCH_LIMIT
            items = _norm(_fetch_search(keyword, news_type, limit), max_summary)
            heading = f"🔍 检索「{keyword}」  {len(items)}条"
            return render(items, heading,
                          {"action": action, "type": news_type, "keyword": keyword,
                           "count": len(items), "items": items})

        if action == "latest":
            if news_type == "all":
                return error("action=latest 需要指定 type（hot/morning/evening）",
                             code="bad_request")
            limit = _as_int(a.get("limit"), DEFAULT_LATEST_LIMIT)
            if limit <= 0:
                limit = DEFAULT_LATEST_LIMIT
            items = _norm(_fetch_latest(news_type, limit), max_summary)
            heading = f"{TYPE_LABELS.get(news_type, news_type)}  最新 {len(items)} 条"
            return render(items, heading,
                          {"action": action, "type": news_type, "count": len(items),
                           "items": items})

        # action == today
        limit = _as_int(a.get("limit"), 0)
        items = _norm(_fetch_today(news_type, limit), max_summary)
        heading = (f"{TYPE_LABELS.get(news_type, news_type)}  {today_str}"
                   if news_type != "all" else f"📰 今日新闻汇总  {today_str}")
        return render(items, heading,
                      {"action": action, "type": news_type, "date": today_str,
                       "count": len(items), "items": items})

    except pymysql.MySQLError as e:
        return error(f"数据库错误: {e}", code="db_error")


if __name__ == "__main__":
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
