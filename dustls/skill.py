# skill: dustls
# description: 磁盘占用分析（dust 的纯数据输出：整数字节 + 全路径，LLM 友好）
# status: enable
# usage:
#   arguments.path  = 目标路径（默认 "."）
#   arguments.paths = 多路径数组（与 path 二选一，如 ["/var","/usr"]）
#   arguments.depth = 目录深度（透传 dust -d）
#   arguments.top   = 全局按体积取前 N 大（祖先自动保留）；0=不限。默认 flat/tree 40，json 不限
#   arguments.all   = true 输出全部（等同 top=0）
#   arguments.tree  = true 输出 {bytes,depth,path}（树形，每层按体积降序）
#   arguments.json  = true 输出嵌套 JSON 树 {bytes,path,children:[...]}
#   arguments.human = true 额外附人类可读体积字段（默认不给）
#   arguments.extra = 透传给 dust 的额外参数数组（如 ["-X","/proc"]）
#   体积一律为整数字节；未安装 dust 时返回 code=not_installed
#   示例: {"path": "/var/log", "top": 20}
#         {"path": "/root", "tree": true, "depth": 2}
#         {"json": true, "path": "/usr"}
#         Windows: {"path": "C:\\\\Users", "top": 20}
"""dustls —— dust 的纯数据包装器（skill 实现，单文件）。

去掉一切给人看的格式（进度条、百分比条、树形符号、列对齐填充、Mi/Gi 人类单位、
终端宽度截断），只输出整数字节 + 全路径的纯数据，直接可进 LLM 上下文：

  flat : 每项 {bytes, path}，按体积降序，全路径不缩短
  tree : 每项 {bytes, depth, path}（depth 0=根），每层内部按体积降序
  json : {bytes, path, children:[...]} 嵌套树，尺寸全为整数

体积一律为整数字节（调用 dust -o b）。`top` 是**全局按体积前 N 大**（不是 dust 那种
先序遍历截断），祖先节点会一并保留，所以 tree/json 剪枝后路径链仍完整。

未安装 dust 时返回 code=not_installed，已安装则直接执行对应命令。
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

_BYTES_RE = re.compile(r"^(\d+)\s*B$")
_U = re.compile(r"^([\d.]+)\s*([KMGTPEZY]?)(i?)(B?)$", re.I)
_UNITS = {"": 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4,
          "P": 1000 ** 5, "E": 1000 ** 6, "Z": 1000 ** 7, "Y": 1000 ** 8}
_BIN_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4,
              "P": 1024 ** 5, "E": 1024 ** 6, "Z": 1024 ** 7, "Y": 1024 ** 8}

_NOT_INSTALLED = ("dust 未安装（Debian/Ubuntu: apt install du-dust；"
                  "macOS: brew install dust；Windows: scoop install dust 或 winget install dust；"
                  "Rust: cargo install du-dust）")


def _dust_bin():
    """定位 dust 可执行文件：优先环境变量 DUST_BIN，其次 PATH。"""
    return os.environ.get("DUST_BIN") or shutil.which("dust")


def _to_bytes(s):
    """把 dust 的 size 字段转成整数字节。"""
    s = (s or "").strip()
    m = _BYTES_RE.match(s)
    if m:                                   # -o b 的正常形态："699146240B"
        return int(m.group(1))
    m = _U.match(s)
    if not m:
        return None
    num, unit, is_bin, b = m.groups()
    unit = unit.upper()
    # "KiB/MiB"=二进制；"KB/MB"=十进制；裸单位(1.2M)按二进制，贴近 du 的块语义
    table = _BIN_UNITS if (is_bin or unit == "" or b == "") else _UNITS
    return int(float(num) * table.get(unit, 1))


def _human(n):
    step = 1024.0
    for u in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < step or u == "PiB":
            return "%d%s" % (n, u) if u == "B" else "%.1f%s" % (n, u)
        n /= step


def _strip_conflicts(extra):
    """剔掉与固定参数冲突的 dust 选项（固定自带 -j -P -o b，重复 -o 会让 dust 报错）。

    返回 (清洗后的参数, 警告列表)。
    """
    out, warnings, skip = [], [], False
    for a in extra:
        if skip:
            skip = False
            continue
        if a in ("-o", "--output-format"):
            skip = True
            warnings.append("忽略 %s（输出固定为整数字节）" % a)
            continue
        if a.startswith("--output-format=") or (a.startswith("-o") and len(a) > 2):
            warnings.append("忽略 %s（输出固定为整数字节）" % a)
            continue
        if a in ("-j", "--output-json"):
            continue
        out.append(a)
    return out, warnings


def _load_dust(binary, depth, extra):
    """跑 dust -j，返回 (JSON 树, 警告)。出错时返回 (None, 错误信息)。"""
    cmd = [binary, "-j", "-P", "-o", "b"]
    if depth is not None:
        cmd += ["-d", str(depth)]
    cmd += extra
    # 固定 UTF-8 解码：dust 恒输出 UTF-8，Windows 中文环境下 text=True 会按 GBK 解码非 ASCII 路径而崩溃
    proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    out, err = proc.stdout, proc.stderr or ""
    first = (err.strip().splitlines() or [""])[0]
    if "No such file or directory" in first or "Permission denied" in first:
        return None, first                     # dust 对坏路径只打 stderr，退出码仍是 0
    idx = out.find("{")
    if idx < 0:
        return None, "dust 未返回 JSON。stdout=%r stderr=%r" % (out[:200], err[:200])
    head = out[:idx].strip()
    if head and ("No such file or directory" in head or "Permission denied" in head):
        return None, head.splitlines()[0]
    try:
        tree = json.loads(out[idx:])
    except json.JSONDecodeError as e:
        return None, "解析 dust JSON 失败: %s" % e
    warn = err.strip().splitlines()[0][:200] if err.strip() else None
    return tree, warn


def _normalize(node, depth, out):
    """转成扁平数组 [{bytes, depth, path, children:[idx...]}]，保持 dust 的遍历顺序。"""
    size = _to_bytes(node.get("size", "0"))
    if size is None:
        raise ValueError("无法解析体积: %r" % node.get("size"))
    idx = len(out)
    out.append({"bytes": size, "depth": depth, "path": node.get("name", ""),
                "children": []})
    for ch in node.get("children") or []:
        out[idx]["children"].append(_normalize(ch, depth + 1, out))
    return idx


def _build_keep(flat, root_idx, top):
    """保留集合 = 全局体积最大的 top 个节点 + 它们的全部祖先（保证路径链完整）。"""
    parent = {root_idx: None}
    stack = [root_idx]
    while stack:
        i = stack.pop()
        for c in flat[i]["children"]:
            parent[c] = i
            stack.append(c)
    cand = [i for i in range(len(flat)) if i != root_idx]
    cand.sort(key=lambda i: (-flat[i]["bytes"], flat[i]["path"]))
    keep = {root_idx}
    for i in cand[:top]:
        keep.add(i)
        p = parent[i]
        while p is not None and p not in keep:
            keep.add(p)
            p = parent[p]
    return keep


def _keep_children(flat, i, keep):
    """已保留的子节点，按体积降序。"""
    kids = [c for c in flat[i]["children"] if c in keep]
    kids.sort(key=lambda c: (-flat[c]["bytes"], flat[c]["path"]))
    return kids


def _prune_json(flat, i, keep, human):
    node = {"bytes": flat[i]["bytes"], "path": flat[i]["path"]}
    if human:
        node["human"] = _human(flat[i]["bytes"])
    node["children"] = [_prune_json(flat, c, keep, human)
                        for c in _keep_children(flat, i, keep)]
    return node


def _collect_paths(a):
    """从 arguments 取出路径列表：paths（数组）优先，其次 path（字符串），默认 '.'。"""
    paths = a.get("paths")
    if isinstance(paths, (list, tuple)):
        paths = [str(p) for p in paths if str(p).strip()]
    elif isinstance(paths, str) and paths.strip():
        paths = [paths]
    else:
        paths = []
    if not paths:
        single = a.get("path")
        if single is not None and str(single).strip():
            paths = [str(single)]
    return paths or ["."]


def _extra_args(a):
    extra = a.get("extra")
    if extra is None:
        return []
    if isinstance(extra, str):
        return shlex.split(extra)
    if isinstance(extra, (list, tuple)):
        return [str(x) for x in extra]
    return []


def run(arguments):
    a = arguments or {}
    binary = _dust_bin()
    if not binary:
        return error(_NOT_INSTALLED, code="not_installed")

    depth = a.get("depth")
    if depth is not None:
        try:
            depth = int(depth)
        except (TypeError, ValueError):
            return error("depth 必须是整数（收到 %r）" % (depth,), code="bad_request")

    warnings = []
    extra, warns = _strip_conflicts(_extra_args(a))
    warnings.extend(warns)
    paths = _collect_paths(a)
    passthrough = extra + paths

    tree, warn = _load_dust(binary, depth, passthrough)
    if tree is None:
        return error(warn, code="dust_failed")
    if warn:
        warnings.append(warn)

    flat = []
    try:
        root_idx = _normalize(tree, 0, flat)
    except ValueError as e:
        return error(str(e), code="bad_response")

    total = flat[root_idx]["bytes"]
    entries = len(flat) - 1

    as_json = bool(a.get("json"))
    as_tree = bool(a.get("tree"))
    human = bool(a.get("human"))

    if a.get("all"):
        top = 0
    elif a.get("top") is None:
        top = 0 if as_json else 40
    else:
        try:
            top = int(a.get("top"))
        except (TypeError, ValueError):
            return error("top 必须是整数（收到 %r）" % (a.get("top"),), code="bad_request")
        top = 0 if top <= 0 else top

    keep = _build_keep(flat, root_idx, top or (entries + 1))

    result = {
        "path": flat[root_idx]["path"],
        "total_bytes": total,
        "entries": entries,
        "rows": len(keep) - 1,
        "format": "json" if as_json else ("tree" if as_tree else "flat"),
        "sort": "size-desc",
    }
    if human:
        result["total_human"] = _human(total)
    if warnings:
        result["warnings"] = warnings

    if as_json:
        result["tree"] = _prune_json(flat, root_idx, keep, human)
    elif as_tree:
        rows = []

        def walk(i):
            n = flat[i]
            row = {"bytes": n["bytes"], "depth": n["depth"], "path": n["path"]}
            if human:
                row["human"] = _human(n["bytes"])
            rows.append(row)
            for c in _keep_children(flat, i, keep):
                walk(c)

        walk(root_idx)
        result["items"] = rows
    else:
        items = [flat[i] for i in range(len(flat)) if i != root_idx and i in keep]
        items.sort(key=lambda n: (-n["bytes"], n["path"]))
        rows = []
        for n in items:
            row = {"bytes": n["bytes"], "path": n["path"]}
            if human:
                row["human"] = _human(n["bytes"])
            rows.append(row)
        result["items"] = rows

    return ok(result)


if __name__ == "__main__":
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
