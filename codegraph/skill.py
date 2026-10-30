# skill: codegraph
# description: 代码图谱检索：符号搜索 / 调用方与被调方 / 影响面 / 任务上下文 / 文件结构 / 建索引
# usage:
#   arguments.action = status（默认）| query | callers | callees | impact | context | files | index | sync | unlock
#   通用: path 项目根（默认 CODEGRAPH_PROJECT 或当前目录）; refresh=true 先增量同步再查
#   query: term 关键词（必填）; limit=10; kind=function|class|method|route|import|variable|file; code=true 附源码
#   callers/callees: symbol（必填）; limit=20; code=true
#   impact: symbol（必填）; depth=2（影响半径）
#   context: task（必填）; max_nodes=20; max_code=5; format=markdown|json
#   files: filter 目录前缀; pattern glob; max_depth; format=tree|flat|grouped
#   index: force=true 全量重建; sync 增量同步; unlock 清卡住的锁; 未建索引先 index
#   示例: {"action":"query","term":"parse_config","code":true}
import os
import sys

try:  # shell-tool：技能以包（skill.<name>.skill）导入，相对导入可用
    from ._codegraph import (
        CodegraphError,
        DEFAULT_TIMEOUT,
        INDEX_TIMEOUT,
        ensure_indexed,
        project_path,
        run_cli,
        run_json,
        status,
        tail,
        trim_node,
    )
except ImportError:  # 其它 runner 以裸模块加载 skill.py，退化为同目录导入
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _codegraph import (  # type: ignore[no-redef]
        CodegraphError,
        DEFAULT_TIMEOUT,
        INDEX_TIMEOUT,
        ensure_indexed,
        project_path,
        run_cli,
        run_json,
        status,
        tail,
        trim_node,
    )

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

_MAX_LIMIT = 100
_ACTIONS = (
    "status", "query", "callers", "callees", "impact",
    "context", "files", "index", "sync", "unlock",
)


def _bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _int(value, default, low=1, high=None):
    if value is None or str(value).strip() == "":
        return default
    number = int(str(value).strip())
    if number < low:
        number = low
    if high is not None and number > high:
        number = high
    return number


def _timeout(a, default=DEFAULT_TIMEOUT):
    return _int(a.get("timeout"), default, low=1, high=3600)


def _term(a):
    for key in ("term", "search", "query", "text"):
        value = a.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _symbol(a):
    value = a.get("symbol") or a.get("name") or ""
    return value.strip() if isinstance(value, str) else ""


def _status_payload(path, data):
    kinds = data.get("nodesByKind") or {}
    return {
        "path": path,
        "initialized": bool(data.get("initialized")),
        "files": data.get("fileCount"),
        "nodes": data.get("nodeCount"),
        "edges": data.get("edgeCount"),
        "db_mb": round((data.get("dbSizeBytes") or 0) / 1048576.0, 1),
        "languages": data.get("languages") or [],
        "nodes_by_kind": kinds,
        "pending": data.get("pendingChanges") or {},
        "worktree_mismatch": data.get("worktreeMismatch"),
    }


def _maybe_sync(a, path, timeout):
    """refresh=true 时先增量同步，返回同步结果（失败不阻断查询）。"""
    if not _bool(a.get("refresh")):
        return None
    try:
        code, out, err = run_cli(["sync", path], timeout=timeout)
        return {"updated": code == 0, "detail": tail(out or err, 200)}
    except CodegraphError as exc:
        return {"updated": False, "detail": str(exc)}


def _act_status(a, path, timeout):
    synced = _maybe_sync(a, path, timeout)
    payload = _status_payload(path, status(path, timeout=timeout))
    if synced is not None:
        payload["synced"] = synced
    return ok(payload)


def _act_query(a, path, timeout):
    term = _term(a)
    if not term:
        return error("缺少 term（要搜索的符号名或关键词）", code="missing_argument")
    ensure_indexed(path, timeout=timeout)
    synced = _maybe_sync(a, path, timeout)
    limit = _int(a.get("limit"), 10, high=_MAX_LIMIT)
    with_code = _bool(a.get("code"))

    args = ["query", "-j", "-p", path, term, "-l", str(limit)]
    kind = (a.get("kind") or "").strip()
    if kind:
        args += ["-k", kind.split(",")[0].strip()]
    rows = run_json(args, timeout=timeout)
    if not isinstance(rows, list):
        rows = rows.get("results") if isinstance(rows, dict) else []

    hits = []
    for row in rows or []:
        node = row.get("node") if isinstance(row, dict) else None
        item = trim_node(node, project=path, with_code=with_code)
        if not item:
            continue
        if isinstance(row, dict) and isinstance(row.get("score"), (int, float)):
            item["score"] = round(row["score"], 1)
        hits.append(item)

    result = {
        "path": path,
        "term": term,
        "count": len(hits),
        "hits": hits,
    }
    if not hits:
        result["hint"] = "没命中：换个更短的符号名，或用 action=files / context 先摸清结构"
    if synced is not None:
        result["synced"] = synced
    return ok(result)


def _act_relations(action, a, path, timeout):
    symbol = _symbol(a)
    if not symbol:
        return error("缺少 symbol（要分析的符号名）", code="missing_argument")
    ensure_indexed(path, timeout=timeout)
    synced = _maybe_sync(a, path, timeout)
    limit = _int(a.get("limit"), 20, high=_MAX_LIMIT)
    with_code = _bool(a.get("code"))

    args = [action, "-j", "-p", path, symbol, "-l", str(limit)]
    data = run_json(args, timeout=timeout)
    if not isinstance(data, dict):
        data = {}
    rows = data.get("callers" if action == "callers" else "callees") or []
    items = [trim_node(row, project=path, with_code=with_code) for row in rows]
    items = [item for item in items if item]

    result = {
        "path": path,
        "symbol": data.get("symbol") or symbol,
        "count": len(items),
        "items": items,
    }
    if not items:
        result["hint"] = (
            "没有记录：可能是入口函数（无调用方），或索引落后（试 refresh=true / action=sync）"
            if action == "callers"
            else "没有记录：可能没有向外调用，或索引落后（试 refresh=true / action=sync）"
        )
    if synced is not None:
        result["synced"] = synced
    return ok(result)


def _act_impact(a, path, timeout):
    symbol = _symbol(a)
    if not symbol:
        return error("缺少 symbol（要评估影响面的符号名）", code="missing_argument")
    ensure_indexed(path, timeout=timeout)
    _maybe_sync(a, path, timeout)
    depth = _int(a.get("depth"), 2, high=6)

    data = run_json(["impact", "-j", "-p", path, symbol, "-d", str(depth)], timeout=timeout)
    if not isinstance(data, dict):
        data = {}
    rows = data.get("affected") or []
    items = [trim_node(row, project=path) for row in rows]
    items = [item for item in items if item]
    return ok({
        "path": path,
        "symbol": data.get("symbol") or symbol,
        "depth": data.get("depth", depth),
        "node_count": data.get("nodeCount", len(items)),
        "edge_count": data.get("edgeCount"),
        "affected": items,
    })


def _act_context(a, path, timeout):
    task = (a.get("task") or a.get("query") or a.get("question") or "").strip()
    if not task:
        return error("缺少 task（要完成的任务描述，如「给登录接口加限流」）", code="missing_argument")
    ensure_indexed(path, timeout=timeout)
    _maybe_sync(a, path, timeout)
    fmt = (a.get("format") or "markdown").strip().lower()
    nodes = _int(a.get("max_nodes"), 20, high=200)
    code_blocks = _int(a.get("max_code"), 5, low=0, high=50)

    args = ["context", "-p", path, task, "-n", str(nodes), "-c", str(code_blocks)]
    if not _bool(a.get("code"), default=True) or code_blocks == 0:
        args.append("--no-code")
    if fmt == "json":
        args += ["-f", "json"]
        data = run_json(args, timeout=timeout)
        nodes_rows = data.get("nodes") if isinstance(data, dict) else []
        return ok({
            "path": path,
            "task": task,
            "summary": (data or {}).get("summary", ""),
            "entry_points": [trim_node(n, project=path) for n in (data or {}).get("entryPoints") or []],
            "nodes": [trim_node(n, project=path) for n in nodes_rows or []],
            "stats": (data or {}).get("stats", {}),
            "related_files": (data or {}).get("relatedFiles") or [],
        })

    code, out, err = run_cli(args, timeout=timeout)
    if code != 0:
        raise CodegraphError("codegraph context 失败：%s" % tail(err or out, 400), code="cli_failed")
    return ok({"path": path, "task": task, "format": "markdown", "markdown": out})


def _act_files(a, path, timeout):
    ensure_indexed(path, timeout=timeout)
    _maybe_sync(a, path, timeout)
    args = ["files", "-j", "-p", path]
    if a.get("filter"):
        args += ["--filter", str(a.get("filter"))]
    if a.get("pattern"):
        args += ["--pattern", str(a.get("pattern"))]
    if a.get("max_depth"):
        args += ["--max-depth", str(_int(a.get("max_depth"), 3, high=20))]

    rows = run_json(args, timeout=timeout)
    if not isinstance(rows, list):
        rows = rows.get("files") if isinstance(rows, dict) else []
    files = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        files.append({
            "path": row.get("path") or "",
            "language": row.get("language") or "",
            "symbols": row.get("nodeCount"),
            "size": row.get("size"),
        })
    return ok({"path": path, "count": len(files), "files": files})


def _act_index(a, path, timeout):
    """建索引：未初始化时先 init（一并完成首次索引），已初始化则普通/强制重建。"""
    force = _bool(a.get("force"))
    initialized = bool(status(path, timeout=timeout).get("initialized"))
    if not initialized:
        args = ["init", "-i", path]
        stage = "init"
    else:
        args = ["index", path]
        if force:
            args.append("--force")
        stage = "index"

    code, out, err = run_cli(args, timeout=timeout)
    detail = tail(out or err, 400)
    if code != 0:
        return error(
            "建索引失败（%s exit %s）：%s（索引卡住可试 action=unlock）" % (stage, code, detail or "无输出"),
            code="index_failed",
        )
    payload = _status_payload(path, status(path, timeout=timeout))
    payload["action"] = "index"
    payload["stage"] = stage
    if stage == "index":
        payload["force"] = force
    payload["detail"] = detail
    return ok(payload)


def _act_sync(a, path, timeout):
    code, out, err = run_cli(["sync", path], timeout=timeout)
    detail = tail(out or err, 400)
    if code != 0:
        return error("增量同步失败（exit %s）：%s" % (code, detail or "无输出"), code="sync_failed")
    payload = _status_payload(path, status(path, timeout=timeout))
    payload["action"] = "sync"
    payload["detail"] = detail
    return ok(payload)


def _act_unlock(a, path, timeout):
    code, out, err = run_cli(["unlock", path], timeout=timeout)
    detail = tail(out or err, 300)
    if code != 0:
        return error("清理索引锁失败（exit %s）：%s" % (code, detail or "无输出"), code="unlock_failed")
    return ok({"path": path, "detail": detail or "已清理"})


_HANDLERS = {
    "status": _act_status,
    "query": _act_query,
    "callers": lambda a, p, t: _act_relations("callers", a, p, t),
    "callees": lambda a, p, t: _act_relations("callees", a, p, t),
    "impact": _act_impact,
    "context": _act_context,
    "files": _act_files,
    "index": _act_index,
    "sync": _act_sync,
    "unlock": _act_unlock,
}


def run(arguments):
    a = arguments or {}
    action = str(a.get("action") or "status").strip().lower()
    handler = _HANDLERS.get(action)
    if not handler:
        return error(
            "未知 action '%s'，可用: %s" % (action, ", ".join(_ACTIONS)),
            code="unknown_action",
        )

    path = project_path(a.get("path"))
    if not os.path.isdir(path):
        return error("path 不是有效目录：%s" % path, code="invalid_argument")

    timeout = _timeout(a, INDEX_TIMEOUT if action in ("index", "sync") else DEFAULT_TIMEOUT)
    try:
        return handler(a, path, timeout)
    except CodegraphError as exc:
        return error(str(exc), code=exc.code)
    except ValueError as exc:
        return error("参数错误：%s" % exc, code="invalid_argument")
    except Exception as exc:  # 兜底：技能异常不能让 runner 崩
        return error("codegraph 调用失败：%s: %s" % (type(exc).__name__, exc), code="internal")


if __name__ == "__main__":
    import json
    import sys

    arg_text = sys.argv[1] if len(sys.argv) > 1 else '{"action": "status"}'
    print(run(json.loads(arg_text)))
