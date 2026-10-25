# skill: mysql
# description: MySQL / MariaDB 客户端（多主机切换 / 只读查询 / 写入 / 元数据 / 诊断，四端通用）
# status: enable
# usage:
#   action = hosts | ping | conn | databases | tables | describe | status | processlist | query | exec
#     查询：query（SELECT/SHOW/DESC/EXPLAIN/WITH；无 LIMIT 自动补，超出提示 truncated）
#     元数据：databases / tables（pattern）/ describe（table 必填，支持 db.table）
#     诊断：ping（逐台连通性/版本/只读）、conn（字符集/时区/最大连接）、status（scope=status|variables）、processlist
#     写：exec（INSERT/UPDATE/DELETE/DDL，须 confirm=true；readonly 主机拒绝）
#   通用参数：host = config 主机名，可数组或 "all"（默认 default_host）；database 覆盖默认库；
#     limit 行数上限（默认 config.max_rows，0=不限）；format = text|json；timeout 秒
#   query / exec 额外：sql（单条；占位符 %s 配 params 数组）
#   readonly 主机：exec 拒绝；query 另拦 SELECT ... INTO OUTFILE/DUMPFILE、FOR UPDATE、
#     LOCK IN SHARE MODE（读接口的写副作用）
#   示例: {"action": "query", "host": "local", "sql": "SELECT * FROM t WHERE id = %s", "params": [7]}
#         {"action": "exec", "host": "local", "sql": "UPDATE t SET a = 1 WHERE id = 7", "confirm": true}
#
# 配置：本目录 config.json（模板 config.example.json）：
#   {"default_host": "local", "max_rows": 200, "timeout": 10,
#    "hosts": {"local": {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "***",
#                        "database": "", "charset": "utf8mb4", "readonly": false}}}
#   无 config.json 时回退环境变量 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB（主机名 env）。
# 依赖：pymysql（纯 Python，不调系统 mysql 命令）；MySQL 8 的 caching_sha2_password 另需 cryptography。
"""多主机 MySQL 客户端 skill：查询 / 写入 / 元数据 / 诊断，四端通用。"""
import json
import os
import re
import time
import unicodedata
from datetime import date, datetime
from decimal import Decimal

try:  # 依赖：pymysql（纯 Python，四端通用）
    import pymysql
except ImportError:  # 运行期由 run() 返回安装提示
    pymysql = None

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SKILL_DIR, "config.json")
DEFAULT_MAX_ROWS = 200
DEFAULT_TIMEOUT = 10
MAX_LIMIT = 5000
CELL_MAX = 48
ALL_KEYS = ("all", "*")
ACTIONS = ("hosts", "ping", "conn", "query", "exec", "databases",
           "tables", "describe", "status", "processlist")
READ_KEYWORDS = ("select", "show", "desc", "describe", "explain", "with", "table", "values")
LIMITABLE_KEYWORDS = ("select", "with", "table", "values")
TITLES = {
    "ping": "连通性测试", "conn": "连接信息", "query": "查询结果", "exec": "执行结果",
    "databases": "数据库列表", "tables": "表列表", "describe": "表结构",
    "status": "状态 / 变量", "processlist": "当前连接",
}
_LITERAL_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
_COMMENT_RE = re.compile(r"--[^\n]*|#[^\n]*|/\*.*?\*/", re.DOTALL)
# 只读主机上 query 也要拦的写副作用：导出文件 / 加锁读
_RISKY_READ_RE = re.compile(
    r"\binto\s+(?:outfile|dumpfile)\b|\bfor\s+update\b|\block\s+in\s+share\s+mode\b",
    re.IGNORECASE)


class _SkillError(Exception):
    """带错误码的业务异常，run() 统一转成 error 信封。"""

    def __init__(self, message, code="invalid_argument"):
        super().__init__(message)
        self.code = code


# ============= 通用小工具 ============
def _as_int(value, default):
    """宽松解析整型，非法值回落默认。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _truthy(value):
    """宽松解析布尔：兼容 JSON 布尔与 'true'/'1' 等字符串。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def _text_width(text):
    """按显示宽度计算（中日韩全角算 2），让表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text, width):
    gap = width - _text_width(text)
    return text + " " * gap if gap > 0 else text


def _clip(text, limit=CELL_MAX):
    """按显示宽度截断长单元格，超出加省略号。"""
    if _text_width(text) <= limit:
        return text
    out, used = [], 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > limit - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "..."


def _cell(value):
    """把 MySQL 值转成 JSON / 表格友好类型（bytes 解码、Decimal 保精度转字符串）。"""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + value.hex()
    if isinstance(value, (datetime, date)):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def _cell_text(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _sql_literal(text):
    """简单字符串字面量转义（仅用于 LIKE 模式的内部拼接）。"""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "''") + "'"


def _quote_ident(name):
    """反引号包裹标识符，支持 db.table 形式。"""
    parts = str(name).split(".")
    if not parts or any(not p.strip() for p in parts):
        raise _SkillError(f"非法标识符：{name}", "invalid_argument")
    return ".".join("`" + p.strip().replace("`", "``") + "`" for p in parts)


def _strip_literals(sql):
    """把字符串字面量替换成空占位，便于做关键字与分号检查。"""
    return _LITERAL_RE.sub("''", sql)


def _masked(sql):
    """字面量 + 注释都抹掉后的 SQL，用于分号 / 关键字 / 危险模式扫描。"""
    return _COMMENT_RE.sub(" ", _strip_literals(sql))


def _leading_keyword(sql):
    """取语句首个关键字（跳过前置 -- / # / /* */ 注释）。"""
    text = sql.lstrip()
    while text:
        if text.startswith("--") or text.startswith("#"):
            nl = text.find("\n")
            if nl < 0:
                return ""
            text = text[nl + 1:].lstrip()
        elif text.startswith("/*"):
            end = text.find("*/")
            if end < 0:
                return ""
            text = text[end + 2:].lstrip()
        else:
            break
    match = re.match(r"[A-Za-z_]+", text)
    return match.group(0).lower() if match else ""


# ============= 配置 =============
def _norm_host(name, item):
    """把 config 里的一条主机配置规整成统一结构。"""
    return {
        "name": name,
        "host": str(item.get("host") or "127.0.0.1").strip(),
        "port": _as_int(item.get("port"), 3306),
        "user": str(item.get("user") or "").strip(),
        "password": "" if item.get("password") is None else str(item.get("password")),
        "database": str(item.get("database") or "").strip(),
        "charset": str(item.get("charset") or "utf8mb4").strip() or "utf8mb4",
        "readonly": _truthy(item.get("readonly")),
    }


def _env_host():
    """无 config.json 时的兜底：环境变量 / shell-tool 的项目根 .env。"""
    try:  # shell-tool：导入 core.config 即加载 .env 并注入 os.environ
        import core.config  # noqa: F401
    except ImportError:
        pass
    host = (os.environ.get("MYSQL_HOST") or "").strip()
    if not host:
        return None
    return _norm_host("env", {
        "host": host,
        "port": os.environ.get("MYSQL_PORT", "3306"),
        "user": os.environ.get("MYSQL_USER", ""),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DB", ""),
    })


def _skill_cfg():
    """读取 config.json（多主机）+ 可选环境变量兜底。"""
    cfg = {"default_host": "", "max_rows": DEFAULT_MAX_ROWS,
           "timeout": DEFAULT_TIMEOUT, "hosts": {}}
    raw = None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = None
    except (OSError, ValueError) as exc:
        raise _SkillError(f"读取 {CONFIG_PATH} 失败：{exc}", "io_error")
    if isinstance(raw, dict):
        cfg["default_host"] = str(raw.get("default_host") or "").strip()
        cfg["max_rows"] = max(0, _as_int(raw.get("max_rows"), DEFAULT_MAX_ROWS))
        cfg["timeout"] = max(1, _as_int(raw.get("timeout"), DEFAULT_TIMEOUT))
        hosts = raw.get("hosts")
        if isinstance(hosts, dict):
            for name, item in hosts.items():
                key = str(name).strip()
                if key and isinstance(item, dict):
                    cfg["hosts"][key] = _norm_host(key, item)
    if not cfg["hosts"]:
        env_host = _env_host()
        if env_host:
            cfg["hosts"]["env"] = env_host
    if cfg["default_host"] not in cfg["hosts"]:
        cfg["default_host"] = next(iter(cfg["hosts"]), "")
    return cfg


def _resolve_hosts(cfg, spec):
    """按 host 参数解析目标主机：缺省=默认主机；'all'=全部；支持数组。"""
    hosts = cfg["hosts"]
    if not hosts:
        raise _SkillError("未配置任何主机：请把 config.example.json 复制为 config.json 并填写 hosts",
                          "not_found")
    if spec is None or (isinstance(spec, str) and not spec.strip()):
        wanted = [cfg["default_host"]]
    elif isinstance(spec, (list, tuple)):
        wanted = [str(x).strip() for x in spec if str(x).strip()]
    else:
        text = str(spec).strip()
        wanted = list(hosts) if text.lower() in ALL_KEYS else [text]
    picked, unknown = [], []
    for name in wanted:
        if name in hosts:
            if hosts[name] not in picked:
                picked.append(hosts[name])
        else:
            unknown.append(name)
    if unknown:
        raise _SkillError(f"未知主机：{', '.join(unknown)}；可用：{', '.join(hosts)}", "not_found")
    return picked


def _limit_of(cfg, args):
    """行数上限：limit 参数 > config.max_rows；0 表示不限，硬上限 MAX_LIMIT。"""
    raw = args.get("limit")
    if raw is None or str(raw).strip() == "":
        return min(cfg["max_rows"], MAX_LIMIT)
    value = _as_int(raw, cfg["max_rows"])
    if value < 0:
        value = cfg["max_rows"]
    return min(value, MAX_LIMIT)


# ============= SQL 栅栏 ============
def _require_sql(args):
    sql = args.get("sql", args.get("query"))
    if not isinstance(sql, str) or not sql.strip():
        raise _SkillError("缺少 sql 参数（单条 SQL 语句）", "invalid_argument")
    body = sql.strip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    return body


def _assert_single(sql):
    """拒绝一次提交多条语句（去掉字面量与注释后，分号后仍有内容即视为多条；
    末尾孤立的分号如 'SELECT 1; -- note' 不算）。"""
    if re.search(r";\s*\S", _masked(sql)):
        raise _SkillError("一次只允许一条语句（检测到分号分隔的多条 SQL）", "invalid_argument")
    return sql


def _apply_limit(sql, limit):
    """只读查询未自带 LIMIT 时补一个，避免整表回传。

    补的是 limit+1：多取的那一行用于可靠地判断结果是否被截断。
    LIMIT 前换行，防止行尾 -- / # 注释把它吞掉。
    """
    if limit <= 0 or _leading_keyword(sql) not in LIMITABLE_KEYWORDS:
        return sql
    if re.search(r"\blimit\b", _masked(sql), re.IGNORECASE):
        return sql
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {int(limit) + 1}"


def _params_of(args):
    raw = args.get("params")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise _SkillError("params 必须是数组（按顺序绑定 %s 占位符）", "invalid_argument")
    return list(raw)


# ============= 连接与执行 ============
def _err_text(exc):
    """把驱动异常翻成一句话，便于 LLM 自纠。"""
    text = str(exc).strip() or type(exc).__name__
    if "cryptography" in text:
        return "MySQL 8 认证需 cryptography：pip install cryptography（或改用 mysql_native_password 账号）"
    if "not all arguments converted" in text or "format string" in text:
        return f"params 数量与 SQL 占位符（%s）不一致：{text}"
    if pymysql is not None and isinstance(exc, pymysql.err.MySQLError):
        args = getattr(exc, "args", ())
        if len(args) >= 2 and isinstance(args[0], int):
            return f"MySQL {args[0]}: {args[1]}"
    if isinstance(exc, OSError) or "timed out" in text.lower():
        return f"网络 / 超时失败：{text}"
    return f"{type(exc).__name__}: {text}"


def _connect(host, database, timeout):
    if not host["user"]:
        raise _SkillError(f"主机 {host['name']} 未配置 user", "invalid_argument")
    wait = timeout or DEFAULT_TIMEOUT
    kwargs = {
        "host": host["host"],
        "port": host["port"],
        "user": host["user"],
        "password": host["password"],
        "charset": host["charset"],
        "autocommit": True,
        "connect_timeout": wait,
        "read_timeout": wait,
        "write_timeout": wait,
    }
    if database:
        kwargs["database"] = database
    return pymysql.connect(**kwargs)


def _run_one(host, sql, database, limit, timeout, params=None):
    """在单台主机执行一条语句；异常只落在本主机结果里，不影响其它主机。"""
    item = {"host": host["name"], "server": f"{host['host']}:{host['port']}"}
    started = time.time()
    try:
        conn = _connect(host, database or host["database"], timeout)
    except Exception as exc:
        item.update({"ok": False, "error": _err_text(exc)})
        item["elapsed_ms"] = int((time.time() - started) * 1000)
        return item
    try:
        with conn.cursor() as cur:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            if cur.description:
                columns = [d[0] for d in cur.description]
                # 多取一行用于判断是否被截断（自动补的 LIMIT 为 n+1），再截回 limit
                rows = cur.fetchmany(limit + 1) if limit > 0 else cur.fetchall()
                truncated = bool(limit > 0 and len(rows) > limit)
                if truncated:
                    rows = rows[:limit]
                item.update({
                    "ok": True,
                    "columns": columns,
                    "rows": [{c: _cell(row[i]) for i, c in enumerate(columns)} for row in rows],
                    "row_count": len(rows),
                    "truncated": truncated,
                })
            else:
                item.update({"ok": True, "columns": [], "rows": [], "row_count": 0,
                             "truncated": False, "affected_rows": cur.rowcount})
    except Exception as exc:
        item.update({"ok": False, "error": _err_text(exc)})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    item["elapsed_ms"] = int((time.time() - started) * 1000)
    return item


def _run_all(hosts, sql, database, limit, timeout, params=None):
    """按主机串行执行：单机失败不影响其它主机。"""
    return [_run_one(h, sql, database, limit, timeout, params) for h in hosts]


# ============= 渲染 ============
def _render_table(columns, rows):
    """等宽文本表格：只按内容实际宽度对齐，不画分隔行，长值截断、NULL 显式标注。"""
    if not columns:
        return ""
    body = [[_cell_text(row.get(c)) for c in columns] for row in rows]
    widths = [min(_text_width(str(c)), CELL_MAX) for c in columns]
    for line in body:
        for i, text in enumerate(line):
            widths[i] = max(widths[i], min(_text_width(text), CELL_MAX))

    def fmt(cells):
        return " | ".join(_pad(_clip(text, widths[i]), widths[i]) for i, text in enumerate(cells))

    out = [fmt([str(c) for c in columns])]
    out.extend(fmt(line) for line in body)
    return "\n".join(out)


def _render(title, results, limit):
    """多主机结果渲染：每台主机一段，行首带元信息，失败单独标注。

    面向 LLM 阅读优化：结构扁平、每段自带"列×行（截断）"摘要，无冗余分隔线。
    """
    ok_count = sum(1 for r in results if r.get("ok"))
    lines = [f"【{title}】{ok_count}/{len(results)} 台成功"]
    for res in results:
        lines.append("")
        head = f"── {res['host']} ({res['server']})"
        if res.get("elapsed_ms") is not None:
            head += f" {res['elapsed_ms']}ms"
        if not res.get("ok"):
            lines += [head, f"  ✗ {res.get('error') or '执行失败'}"]
            continue
        if not res.get("columns"):
            lines += [head, f"  ✓ 影响 {res.get('affected_rows', 0)} 行"]
            continue
        meta = f"{len(res['columns'])}列×{res['row_count']}行"
        if res.get("truncated"):
            meta += "（截断）"
        lines.append(head + " · " + meta)
        lines.append(_render_table(res["columns"], res["rows"]) or "  (0 行)")
        if res.get("truncated"):
            lines.append(f"  … 仅前 {res['row_count']} 行（limit={limit}；可加 WHERE 或调大 limit）")
    return "\n".join(lines).strip()


# ============= 各 action 的 SQL ============
# 兼容 MySQL 5.6+ / 8.x 与 MariaDB / TiDB：只用两端都存在的系统变量。
# 注意 MariaDB 没有 @@super_read_only（用它会报 ERROR 1193），故不纳入。
_PING_SQL = ("SELECT VERSION() AS version, DATABASE() AS current_db, "
             "@@read_only AS read_only, CONNECTION_ID() AS connection_id")
_CONN_SQL = ("SELECT VERSION() AS version, @@version_comment AS version_comment, "
             "DATABASE() AS current_db, @@character_set_server AS charset, "
             "@@collation_server AS collation, @@time_zone AS time_zone, "
             "@@system_time_zone AS system_time_zone, @@max_connections AS max_connections, "
             "@@lower_case_table_names AS lower_case_table_names, "
             "NOW() AS server_now, UNIX_TIMESTAMP() AS server_ts")


def _build(action, args):
    """把元数据类 action + 参数编成 (sql, params)。"""
    if action == "ping":
        return _PING_SQL, None
    if action == "conn":
        return _CONN_SQL, None
    if action == "databases":
        return "SHOW DATABASES", None
    if action == "processlist":
        return "SHOW FULL PROCESSLIST", None
    if action == "tables":
        pattern = str(args.get("pattern") or args.get("like") or "").strip()
        sql = "SHOW TABLES"
        if pattern:
            like = pattern if "%" in pattern or "_" in pattern else f"%{pattern}%"
            sql += " LIKE " + _sql_literal(like)
        return sql, None
    if action == "describe":
        table = str(args.get("table") or "").strip()
        if not table:
            raise _SkillError("缺少 table 参数（支持 db.table）", "invalid_argument")
        return f"SHOW FULL COLUMNS FROM {_quote_ident(table)}", None
    if action == "status":
        scope = str(args.get("scope") or "status").strip().lower()
        if scope in ("variable", "variables", "vars"):
            scope = "variables"
        elif scope in ("status", "stats", "state"):
            scope = "status"
        else:
            raise _SkillError("scope 只能是 status 或 variables", "invalid_argument")
        sql = f"SHOW GLOBAL {scope.upper()}"
        like = str(args.get("like") or args.get("pattern") or "").strip()
        if like:
            sql += " LIKE " + _sql_literal(like if "%" in like else f"%{like}%")
        return sql, None
    raise _SkillError(f"action {action} 不支持直接构建 SQL", "invalid_argument")


def _action_hosts(cfg, fmt):
    """hosts：列出已配置主机（永不返回密码）。"""
    items = []
    for name, host in cfg["hosts"].items():
        items.append({
            "name": name,
            "server": f"{host['host']}:{host['port']}",
            "user": host["user"] or "(未配置)",
            "database": host["database"],
            "charset": host["charset"],
            "readonly": host["readonly"],
            "default": name == cfg["default_host"],
        })
    if not items:
        raise _SkillError("未配置任何主机：请把 config.example.json 复制为 config.json 并填写 hosts",
                          "not_found")
    payload = {"total": len(items), "default_host": cfg["default_host"], "hosts": items}
    if fmt == "json":
        return ok(payload)
    lines = [f"已配置 {len(items)} 个主机（默认：{cfg['default_host'] or '未设置'}；"
             f"max_rows={cfg['max_rows']}，timeout={cfg['timeout']}s）", ""]
    for it in items:
        flags = (" [默认]" if it["default"] else "") + (" [只读]" if it["readonly"] else "")
        db = f"  db={it['database']}" if it["database"] else ""
        lines.append(f"- {it['name']}{flags}  {it['server']}  user={it['user']}{db}")
    lines += ["", "用法示例：{\"action\": \"ping\", \"host\": \"all\"}"]
    return ok({"output": "\n".join(lines)})


# ============= 入口 ============
def run(arguments):
    args = arguments if isinstance(arguments, dict) else {}
    action = str(args.get("action") or "hosts").strip().lower()
    if action not in ACTIONS:
        return error(f"action 必须是 {' / '.join(ACTIONS)} 之一", code="invalid_argument")
    fmt = "json" if str(args.get("format") or "text").strip().lower() == "json" else "text"
    try:
        cfg = _skill_cfg()
        if action == "hosts":
            return _action_hosts(cfg, fmt)
        if pymysql is None:
            return error("需要 pymysql：pip install pymysql（纯 Python，四端通用）", code="internal")

        hosts = _resolve_hosts(cfg, args.get("host"))
        limit = _limit_of(cfg, args)
        database = str(args.get("database") or "").strip()
        timeout = max(1, _as_int(args.get("timeout"), cfg["timeout"]))

        if action == "query":
            sql = _assert_single(_require_sql(args))
            if _leading_keyword(sql) not in READ_KEYWORDS:
                return error("query 只接受只读语句（SELECT / SHOW / DESC / EXPLAIN / WITH）；"
                             "写操作请用 action=exec", code="invalid_argument")
            locked = [h["name"] for h in hosts if h["readonly"]]
            if locked and _RISKY_READ_RE.search(_masked(sql)):
                return error("主机为只读（config readonly=true），该语句含写副作用"
                             "（INTO OUTFILE / FOR UPDATE / LOCK IN SHARE MODE）："
                             + ", ".join(locked), code="invalid_argument")
            results = _run_all(hosts, _apply_limit(sql, limit), database, limit, timeout,
                               _params_of(args))
        elif action == "exec":
            sql = _assert_single(_require_sql(args))
            if _leading_keyword(sql) in READ_KEYWORDS:
                return error("exec 只接受写语句（INSERT / UPDATE / DELETE / DDL）；"
                             "只读查询请用 action=query", code="invalid_argument")
            if not _truthy(args.get("confirm")):
                return error("写操作需显式 confirm=true（请先确认 SQL 与目标主机）",
                             code="invalid_argument")
            locked = [h["name"] for h in hosts if h["readonly"]]
            if locked:
                return error(f"主机为只读（config readonly=true）：{', '.join(locked)}",
                             code="invalid_argument")
            results = _run_all(hosts, sql, database, 0, timeout, _params_of(args))
        else:
            sql, params = _build(action, args)
            results = _run_all(hosts, sql, database, limit, timeout, params)

        if fmt == "json":
            return ok({"action": action, "hosts": [h["name"] for h in hosts],
                       "limit": limit, "results": results})
        return ok({"output": _render(TITLES.get(action, action), results, limit)})
    except _SkillError as exc:
        return error(str(exc), code=exc.code)
    except Exception as exc:  # 兜底：不把栈丢给 LLM
        return error(_err_text(exc), code="internal")
