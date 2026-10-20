# skill: storage_audit
# description: 手机存储审计：容量 / 最大文件 / 目录与类型排行 / 回收站清理（仅 Android）
# status: enable
# usage:
#   arguments.action = overview(默认) | top_files | dirs | types | trash
#   arguments.path   = 扫描根目录，默认 /storage/emulated/0
#   arguments.limit  = 条数，默认 20
#   arguments.depth  = dirs 层级，默认 1
#   arguments.min_mb / ext / older_days = 文件过滤条件
#   arguments.refresh = true 忽略缓存（默认 10 分钟）
#   arguments.delete  = true 仅 trash：删除回收站残留，不可恢复
#   示例: {}   {"action":"top_files","limit":5,"min_mb":50}   {"action":"trash","delete":true}
"""storage_audit —— 手机存储审计（仅 Android）。

扫描 root 建立文件索引并缓存 10 分钟，输出 Markdown：分区容量、目录/类型排行、
最大文件、回收站残留。trash 动作可清理回收站残留（仅 .trashed-* 与常见回收站
目录内文件），删除前会做根目录越界校验。Android/data 不可读，只统计用户文件区。
"""
import hashlib
import json
import os
import sys
import time

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

DEFAULT_ROOT = "/storage/emulated/0"
CACHE_TTL = 600          # 缓存有效期（秒）
TIME_BUDGET = 25.0       # 单次扫描时间上限（秒），超出即返回部分结果
MAX_FILES = 400000       # 索引文件数上限，防止 OOM
MAX_CACHE_FILES = 60000  # 超过此数量不落盘缓存（序列化太慢）
TRASH_DIRS = (".MediaTrash", ".FileManagerRecycler", ".SoundRecordRecycler",
              ".estrongs/.trash", ".estrongs/.recycle")
TRASH_PREFIX = ".trashed"

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".storage_audit")


def _fmt(n):
    n = float(n)
    if n >= 1 << 30:
        return "%.2f GB" % (n / (1 << 30))
    if n >= 1 << 20:
        return "%.1f MB" % (n / (1 << 20))
    if n >= 1 << 10:
        return "%.0f KB" % (n / (1 << 10))
    return "%d B" % n


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _norm(path):
    return path.replace("\\", "/")


def _inside(full, rootp):
    """full 是否在 rootp 之内（均为规范化绝对路径）"""
    full = _norm(full).rstrip("/")
    if not rootp:
        return full.startswith("/")
    return full == rootp or full.startswith(rootp + "/")


def _partition(path):
    """返回分区的总/已用/可用字节数（statvfs，非 Unix 平台返回 None）"""
    fn = getattr(os, "statvfs", None)
    if fn is None:
        return None
    try:
        v = fn(path)
    except OSError:
        return None
    frs = v.f_frsize
    total = v.f_blocks * frs
    free = v.f_bavail * frs
    used = total - free
    return {"total": total, "used": used, "free": free,
            "pct": round(used * 100.0 / total, 1) if total else 0.0}


def _scan(root):
    """遍历 root，返回 [[path, size, mtime], ...] 及扫描元信息"""
    t0 = time.time()
    files = []
    errors = 0
    truncated = False
    stop = False
    stack = [root]
    while stack and not stop:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if time.time() - t0 > TIME_BUDGET:
                        truncated = stop = True
                        break
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            files.append([e.path, st.st_size, int(st.st_mtime)])
                            if len(files) >= MAX_FILES:
                                truncated = stop = True
                                break
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1  # 权限不足（Android/data、应用私有目录等）
    return files, round(time.time() - t0, 2), errors, truncated


def _cache_file(root):
    h = hashlib.md5(root.encode("utf-8")).hexdigest()[:12]
    return os.path.join(CACHE_DIR, "idx_%s.json" % h)


def _get_index(root, refresh):
    """带缓存的索引加载，返回 (files, meta)"""
    cp = _cache_file(root)
    if not refresh:
        try:
            with open(cp, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("root") == root and time.time() - d.get("ts", 0) < CACHE_TTL:
                return d["files"], {"secs": d["secs"], "errors": d["errors"],
                                    "truncated": d["truncated"], "cached": True,
                                    "scan_ts": d["ts"]}
        except Exception:
            pass
    files, secs, errors, truncated = _scan(root)
    meta = {"secs": secs, "errors": errors, "truncated": truncated,
            "cached": False, "scan_ts": time.time()}
    if len(files) <= MAX_CACHE_FILES:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cp, "w", encoding="utf-8") as f:
                json.dump({"root": root, "ts": meta["scan_ts"], "secs": secs,
                           "errors": errors, "truncated": truncated,
                           "files": files}, f)
        except Exception:
            pass
    return files, meta


def _rel(path, root):
    rootp = _norm(root).rstrip("/")
    p = _norm(path)
    if p == rootp:
        return "."
    if p.startswith(rootp + "/"):
        return p[len(rootp) + 1:]
    return p


def _dir_sizes(files, root):
    """累计每个祖先目录的体积"""
    rootp = _norm(root).rstrip("/")
    agg = {}
    for p, s, _ in files:
        rel = _rel(p, rootp)
        parts = rel.split("/")[:-1]
        cur = rootp
        for part in parts:
            cur = cur + "/" + part
            agg[cur] = agg.get(cur, 0) + s
    return agg


def _is_trash(path):
    p = _norm(path)
    name = p.rsplit("/", 1)[-1]
    if name.startswith(TRASH_PREFIX):
        return True
    return any(("/" + d + "/") in p for d in TRASH_DIRS)


def _collect_trash(files, root):
    items = []
    total = 0
    for p, s, m in files:
        if _is_trash(p):
            items.append({"path": _rel(p, root), "size": s, "size_h": _fmt(s),
                          "mtime": time.strftime("%Y-%m-%d", time.localtime(m))})
            total += s
    items.sort(key=lambda x: -x["size"])
    return {"count": len(items), "size": total, "size_h": _fmt(total), "items": items}


def _agg_types(files, limit=20):
    agg = {}
    for p, s, _ in files:
        ext = os.path.splitext(p)[1].lower().lstrip(".") or "(无扩展名)"
        a = agg.setdefault(ext, [0, 0])
        a[0] += 1
        a[1] += s
    out = [{"ext": k, "count": v[0], "size": v[1], "size_h": _fmt(v[1])}
           for k, v in agg.items()]
    out.sort(key=lambda x: -x["size"])
    return out[:limit]


def _filter_files(files, root, min_mb=0.0, ext=None, older_days=None):
    min_b = int(max(0.0, min_mb) * (1 << 20))
    ext = (ext or "").strip().lower().lstrip(".")
    cutoff = None
    if older_days:
        cutoff = time.time() - float(older_days) * 86400
    out = []
    for p, s, m in files:
        if s < min_b:
            continue
        if ext and os.path.splitext(p)[1].lower().lstrip(".") != ext:
            continue
        if cutoff and m > cutoff:
            continue
        out.append({"path": _rel(p, root), "size": s, "size_h": _fmt(s),
                    "mtime": time.strftime("%Y-%m-%d", time.localtime(m))})
    out.sort(key=lambda x: -x["size"])
    return out


def _md_overview(d):
    p = d["partition"]
    L = []
    L.append("## 存储概览（%s）" % d["root"])
    L.append("")
    if p:
        L.append("| 容量 | 已用 | 可用 | 使用率 |")
        L.append("|---|---|---|---|")
        L.append("| %.0f GB | %.0f GB | %.0f GB | %.0f%% |"
                 % (p["total"] / (1 << 30), p["used"] / (1 << 30),
                    p["free"] / (1 << 30), p["pct"]))
        L.append("")
    L.append("用户文件区：**%s**（%d 个文件%s）"
             % (d["user_files_h"], d["scanned_files"],
                "，已截断" if d["truncated"] else ""))
    if d["top_dirs"]:
        L.append("")
        L.append("### 目录占用")
        L.append("")
        L.append("| 目录 | 占用 |")
        L.append("|---|---|")
        for x in d["top_dirs"]:
            L.append("| %s | %s |" % (x["path"], x["size_h"]))
    if d["types"]:
        L.append("")
        L.append("### 文件类型")
        L.append("")
        L.append("| 类型 | 数量 | 体积 |")
        L.append("|---|---|---|")
        for x in d["types"][:8]:
            L.append("| %s | %d | %s |" % (x["ext"], x["count"], x["size_h"]))
    if d["largest"]:
        L.append("")
        L.append("### 最大文件")
        L.append("")
        L.append("| 大小 | 修改日期 | 文件 |")
        L.append("|---|---|---|")
        for x in d["largest"]:
            L.append("| %s | %s | %s |" % (x["size_h"], x["mtime"], x["path"]))
    t = d["trash"]
    L.append("")
    L.append("回收站残留 **%d** 个文件，共 **%s**" % (t["count"], t["size_h"]))
    return "\n".join(L)


def _md_table(title, rows, cols):
    if not rows:
        return "%s\n\n（无）" % title
    L = [title, ""]
    L.append("| " + " | ".join(cols) + " |")
    L.append("|" + "---|" * len(cols))
    for r in rows:
        L.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(L)


def run(arguments):
    a = arguments if isinstance(arguments, dict) else {}

    raw_action = a.get("action")
    action = str(raw_action).strip().lower() if raw_action is not None else ""
    action = action or "overview"
    if action not in ("overview", "top_files", "dirs", "types", "trash"):
        return error("未知 action: %s（可用 overview / top_files / dirs / types / trash）" % action,
                     code="invalid_argument")

    root = a.get("path") or DEFAULT_ROOT
    if not isinstance(root, str) or not root.startswith("/"):
        return error("path 必须是绝对路径", code="invalid_argument")
    root = root.rstrip("/") or "/"
    if not os.path.isdir(root):
        return error("目录不存在或不可读: %s" % root, code="not_found")

    try:
        limit = max(1, min(int(a.get("limit") or 20), 200))
    except (TypeError, ValueError):
        return error("limit 必须是整数", code="invalid_argument")
    try:
        depth = max(1, min(int(a.get("depth") if a.get("depth") is not None else 1), 8))
    except (TypeError, ValueError):
        return error("depth 必须是整数", code="invalid_argument")
    try:
        min_mb = max(0.0, float(a.get("min_mb") or 0))
    except (TypeError, ValueError):
        return error("min_mb 必须是数字", code="invalid_argument")

    ext = a.get("ext")
    if ext is not None and not isinstance(ext, str):
        return error("ext 必须是字符串", code="invalid_argument")

    older_days = a.get("older_days")
    if older_days in (None, ""):
        older_days = None
    else:
        try:
            older_days = float(older_days)
        except (TypeError, ValueError):
            return error("older_days 必须是数字", code="invalid_argument")
        if older_days < 0:
            return error("older_days 不能为负数", code="invalid_argument")

    files, meta = _get_index(root, _truthy(a.get("refresh")))

    # ---------- trash ----------
    if action == "trash":
        trash = _collect_trash(files, root)
        items = list(trash["items"])
        deleted = []
        failed = []
        if _truthy(a.get("delete")):
            rootp = _norm(os.path.realpath(root)).rstrip("/")
            for it in items:
                full = _norm(os.path.realpath(os.path.join(root, it["path"])))
                if not _inside(full, rootp) or not _is_trash(full) or not os.path.isfile(full):
                    continue
                try:
                    os.remove(full)
                    deleted.append(it)
                except OSError as exc:
                    it["error"] = str(exc)
                    failed.append(it)
            if deleted:
                try:
                    os.remove(_cache_file(root))
                except OSError:
                    pass

        gone = {id(x) for x in deleted}
        remaining = [x for x in items if id(x) not in gone]
        remain_size = sum(x["size"] for x in remaining)
        md = _md_table("## 回收站残留（%d 个 / %s）" % (len(remaining), _fmt(remain_size)),
                       [(x["size_h"], x["mtime"], x["path"]) for x in remaining[:limit]],
                       ["大小", "日期", "文件"])
        if _truthy(a.get("delete")):
            md += "\n\n已删除 **%d** 个文件，释放 **%s**" % (
                len(deleted), _fmt(sum(x["size"] for x in deleted)))
            if failed:
                md += "；失败 **%d** 个" % len(failed)
                for x in failed[:10]:
                    md += "\n- %s（%s）" % (x["path"], x["error"])
        else:
            md += "\n\n（仅统计；删除用 delete=true）"
        return ok({"markdown": md})

    # ---------- top_files ----------
    if action == "top_files":
        rows = _filter_files(files, root, min_mb=min_mb, ext=ext,
                             older_days=older_days)[:limit]
        total = sum(x["size"] for x in rows)
        md = _md_table("## 最大文件 Top %d（合计 %s）" % (len(rows), _fmt(total)),
                       [(x["size_h"], x["mtime"], x["path"]) for x in rows],
                       ["大小", "日期", "文件"])
        return ok({"markdown": md})

    # ---------- dirs ----------
    if action == "dirs":
        agg = _dir_sizes(files, root)
        rootp = root.rstrip("/")
        rows = []
        for k, v in agg.items():
            rel = _rel(k, rootp)
            if rel.count("/") != depth - 1:
                continue
            rows.append({"path": rel + "/", "size": v, "size_h": _fmt(v)})
        rows.sort(key=lambda x: -x["size"])
        rows = rows[:limit]
        return ok({"markdown": _md_table(
            "## 目录占用（深度 %d）" % depth,
            [(x["size_h"], x["path"]) for x in rows], ["占用", "目录"])})

    # ---------- types ----------
    if action == "types":
        rows = _agg_types(files, limit=limit)
        return ok({"markdown": _md_table(
            "## 文件类型占用",
            [(x["ext"], x["count"], x["size_h"]) for x in rows],
            ["类型", "数量", "体积"])})

    # ---------- overview（默认） ----------
    part = _partition(root)
    agg = _dir_sizes(files, root)
    rootp = root.rstrip("/")
    dirs_top = []
    for k, v in agg.items():
        rel = _rel(k, rootp)
        if "/" in rel:
            continue
        dirs_top.append({"path": rel + "/", "size": v, "size_h": _fmt(v)})
    dirs_top.sort(key=lambda x: -x["size"])
    dirs_top = dirs_top[:8]
    types = _agg_types(files, limit=10)
    largest = _filter_files(files, root)[:8]
    trash = _collect_trash(files, root)
    user_bytes = sum(f[1] for f in files)
    res = {
        "root": root, "partition": part,
        "scanned_files": len(files), "user_files_h": _fmt(user_bytes),
        "truncated": meta["truncated"],
        "top_dirs": dirs_top, "types": types, "largest": largest,
        "trash": {"count": trash["count"], "size": trash["size"],
                  "size_h": trash["size_h"]},
    }
    return ok({"markdown": _md_overview(res)})


if __name__ == "__main__":  # 本地调试：python skill.py '{"action":"overview"}'
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
