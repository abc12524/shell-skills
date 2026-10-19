# skill: smb
# description: SMB/CIFS 文件传输（基于系统 smbclient）：文件/文件夹上传、下载、删除、列表
# status: enable
# usage:
#   arguments.action = upload | download | delete | list
#     upload    上传文件/文件夹：local_path -> remote_path
#     download  下载文件/文件夹：remote_path -> local_path
#     delete    删除文件/文件夹：remote_path（删除非空目录需 recursive=true）
#     list      列出目录：remote_path（默认共享根）
#   连接参数（也可用同目录 config.json / 环境变量 SMB_* 提供）：
#     arguments.host     主机名或 IP（必填）
#     arguments.share    共享名（必填）
#     arguments.username 用户名（可写 domain\\user / user@domain；省略则匿名 -N）
#     arguments.password 密码（默认空 / 匿名）
#     arguments.domain   域 / 工作组（可省略）
#     arguments.port     端口（默认 445）
#     arguments.timeout  单个 smbclient 调用的超时秒数（默认 60）
#   路径参数：
#     arguments.remote_path 共享内路径，如 "dir/file.txt"（相对共享根）
#     arguments.local_path  本机路径（绝对或相对当前目录）
#     arguments.recursive   删除目录时是否递归（默认 false；upload/download 目录恒递归）
#   说明: 依赖系统 smbclient 可执行文件（Debian/Ubuntu: samba-client；RHEL: samba-client；
#         Windows: 需自行提供 smbclient.exe）。
#         密码通过环境变量 PASSWD 传递，不出现在命令行参数中；列表解析对含连续空格的文件名
#         可能不准，目录传输使用 smbclient 原生 mget/mput 递归。
#   示例: {"action": "list", "host": "192.168.1.10", "share": "data", "username": "u", "password": "p"}
#         {"action": "upload", "local_path": "./a.txt", "remote_path": "docs/a.txt", ...}
#         {"action": "download", "remote_path": "docs", "local_path": "./dst", ...}
#         {"action": "delete", "remote_path": "docs", "recursive": true, ...}
import json
import os
import re
import shutil
import subprocess
import sys

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

DEFAULT_TIMEOUT = 60
_LS_RE = re.compile(r"^ {2}(?P<name>.*?)\s{2,}(?P<attrs>[A-Za-z]{1,8})\s+(?P<size>\d+)\s+\S")


class SMBError(Exception):
    def __init__(self, message, code="smb_error"):
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
        if isinstance(value, str):
            os.environ.setdefault(key, value)


def _split_account(username, domain):
    user = str(username or "").strip()
    dom = str(domain or "").strip()
    if "\\" in user:
        dom, user = [part.strip() for part in user.split("\\", 1)]
    elif "@" in user and not dom:
        user, dom = [part.strip() for part in user.split("@", 1)]
    return user, dom


def _normalize_remote(value):
    text = str(value or "").replace("/", "\\").strip()
    parts = []
    for part in text.split("\\"):
        if part in ("", "."):
            continue
        if part == "..":
            raise SMBError("remote_path 不允许包含 '..'", "invalid_argument")
        parts.append(part)
    return "\\".join(parts)


def _remote_join(parent, name):
    if not parent:
        return name
    return parent + "\\" + name


def _quote(value):
    text = str(value)
    if '"' in text:
        raise SMBError('路径不支持包含双引号: %r' % text, "invalid_argument")
    return '"' + text + '"'


class SmbClient:
    def __init__(self, host, share, username, password, domain="", port=445, timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.share = share.strip("\\/")
        self.username, self.domain = _split_account(username, domain)
        self.password = password or ""
        self.port = int(port)
        self.timeout = float(timeout)
        self.binary = shutil.which("smbclient")

    def _command(self, script):
        if not self.binary:
            raise SMBError("未找到 smbclient，请先安装（Debian/Ubuntu: samba-client，RHEL: samba-client）",
                           "not_installed")
        cmd = [self.binary, "//%s/%s" % (self.host, self.share), "-p", str(self.port)]
        if self.username:
            cmd += ["-U", self.username]
        else:
            cmd += ["-N"]
        if self.domain:
            cmd += ["-W", self.domain]
        if not self.password and self.username:
            cmd += ["-N"]
        cmd += ["-c", script]
        return cmd

    def run(self, script, strict=True):
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["PASSWD"] = self.password
        try:
            proc = subprocess.run(self._command(script), capture_output=True, text=True,
                                  errors="replace", timeout=self.timeout, env=env,
                                  stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise SMBError("未找到 smbclient，请先安装（samba-client）", "not_installed")
        except subprocess.TimeoutExpired:
            raise SMBError("smbclient 执行超时（%ss）" % self.timeout, "timeout")
        except OSError as exc:
            raise SMBError("无法启动 smbclient: %s" % exc, "io_error")
        output = (proc.stdout or "") + (proc.stderr or "")
        status_line = _extract_error(output)
        if _is_fatal(status_line):
            raise SMBError("smbclient 失败: %s" % status_line, _status_code(status_line))
        if proc.returncode != 0:
            detail = status_line or (proc.stderr or "").strip() or "退出码 %d" % proc.returncode
            if strict or not status_line:
                raise SMBError("smbclient 失败: %s" % detail, _status_code(detail))
            return output
        if strict and status_line:
            raise SMBError("操作失败: %s" % status_line, _status_code(status_line))
        return output

    def mkdir_p(self, path):
        if not path:
            return
        prefix = ""
        commands = []
        for part in path.split("\\"):
            if not part:
                continue
            prefix = part if not prefix else prefix + "\\" + part
            commands.append("mkdir " + _quote(prefix))
        self.run("; ".join(commands), strict=False)

    def probe(self, path):
        if not path:
            return "dir"
        if "NT_STATUS_" not in self.run("cd " + _quote(path), strict=False):
            return "dir"
        if "NT_STATUS_" not in self.run("ls " + _quote(path), strict=False):
            return "file"
        return "missing"

    def list_entries(self, path, is_dir=True):
        if is_dir:
            commands = []
            if path:
                commands.append("cd " + _quote(path))
            commands.append("ls")
            script = "; ".join(commands)
        else:
            script = "ls " + _quote(path)
        output = self.run(script, strict=False)
        if _extract_error(output):
            raise SMBError("列举失败: %s" % _extract_error(output), _status_code(_extract_error(output)))
        entries = []
        for line in output.splitlines():
            match = _LS_RE.match(line)
            if not match:
                continue
            name = match.group("name").strip()
            if name in ("", ".", ".."):
                continue
            entries.append({
                "name": name,
                "is_dir": "D" in match.group("attrs").upper(),
                "size": int(match.group("size")),
                "attributes": match.group("attrs"),
            })
        return entries

    def delete_tree(self, path, recursive):
        entries = self.list_entries(path)
        if entries and not recursive:
            raise SMBError("目录非空，需 recursive=true: %s" % path, "directory_not_empty")
        files = 0
        dirs = 0
        for entry in entries:
            child = _remote_join(path, entry["name"])
            if entry["is_dir"]:
                sub_files, sub_dirs = self.delete_tree(child, recursive)
                files += sub_files
                dirs += sub_dirs
            else:
                self.run("del " + _quote(child))
                files += 1
        self.run("rmdir " + _quote(path))
        return files, dirs + 1


def _extract_error(output):
    for line in output.splitlines():
        if "NT_STATUS_" in line:
            return line.strip()
    return ""


_FATAL_STATUSES = (
    "LOGON_FAILURE", "PASSWORD_EXPIRED", "PASSWORD_MUST_CHANGE", "ACCOUNT_DISABLED",
    "ACCOUNT_RESTRICTION", "LOGON_TYPE_NOT_GRANTED", "BAD_NETWORK_NAME",
    "NETWORK_NAME_DELETED", "NO_LOGON_SERVERS", "ACCESS_DENIED",
)


def _is_fatal(text):
    return bool(text) and any(marker in text for marker in _FATAL_STATUSES)


def _status_code(text):
    if "LOGON_FAILURE" in text or "PASSWORD" in text:
        return "logon_failure"
    if "ACCESS_DENIED" in text:
        return "access_denied"
    if "OBJECT_NAME_NOT_FOUND" in text or "OBJECT_PATH_NOT_FOUND" in text:
        return "not_found"
    if "DIRECTORY_NOT_EMPTY" in text:
        return "directory_not_empty"
    if "OBJECT_NAME_COLLISION" in text or "OBJECT_NAME_EXISTS" in text:
        return "already_exists"
    if "SHARING_VIOLATION" in text:
        return "sharing_violation"
    if "BAD_NETWORK_NAME" in text:
        return "bad_network_name"
    if "NOT_A_DIRECTORY" in text:
        return "not_a_directory"
    if "IS_A_DIRECTORY" in text:
        return "is_directory"
    return "smb_error"


def _connection(a):
    host = str(a.get("host") or os.environ.get("SMB_HOST") or "").strip()
    share = str(a.get("share") or os.environ.get("SMB_SHARE") or "").strip()
    username = a.get("username") or os.environ.get("SMB_USERNAME")
    password = a.get("password")
    if password is None:
        password = os.environ.get("SMB_PASSWORD", "")
    domain = a.get("domain")
    if domain is None:
        domain = os.environ.get("SMB_DOMAIN", "")
    port = a.get("port") or os.environ.get("SMB_PORT") or 445
    timeout = a.get("timeout") or DEFAULT_TIMEOUT
    if not host:
        raise SMBError("缺少 host（或环境变量 SMB_HOST）", "invalid_argument")
    if not share:
        raise SMBError("缺少 share（或环境变量 SMB_SHARE）", "invalid_argument")
    try:
        port = int(port)
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise SMBError("port/timeout 必须是数字", "invalid_argument")
    return SmbClient(host, share, username, str(password), domain, port, timeout)


def _local_path(a, key):
    raw = a.get(key)
    if raw is None or str(raw).strip() == "":
        raise SMBError("缺少 %s" % key, "invalid_argument")
    return os.path.abspath(os.path.expanduser(os.fspath(raw)))


def _walk_stats(root):
    files = 0
    bytes_total = 0
    dirs = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirs += len(dirnames)
        for name in filenames:
            files += 1
            try:
                bytes_total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return files, dirs, bytes_total


def _do_upload(client, a):
    local = _local_path(a, "local_path")
    if not os.path.exists(local):
        raise SMBError("本地路径不存在: %s" % local, "not_found")
    remote = _normalize_remote(a.get("remote_path"))
    trailing = str(a.get("remote_path") or "").endswith(("/", "\\"))
    if os.path.isdir(local):
        if not remote or trailing:
            remote = _remote_join(remote, os.path.basename(local.rstrip(os.sep)))
        client.mkdir_p(remote)
        commands = ["lcd " + _quote(local), "prompt OFF", "recurse ON"]
        if remote:
            commands.append("cd " + _quote(remote))
        commands.append("mput *")
        client.run("; ".join(commands))
        files, dirs, bytes_total = _walk_stats(local)
        return ok({"action": "upload", "remote_path": remote, "local_path": local,
                   "files": files, "dirs": dirs, "bytes": bytes_total})
    if not remote or trailing:
        remote = _remote_join(remote, os.path.basename(local))
    client.mkdir_p(remote.rsplit("\\", 1)[0] if "\\" in remote else "")
    client.run("put %s %s" % (_quote(local), _quote(remote)))
    return ok({"action": "upload", "remote_path": remote, "local_path": local,
               "files": 1, "dirs": 0, "bytes": os.path.getsize(local)})


def _do_download(client, a):
    remote = _normalize_remote(a.get("remote_path"))
    if not remote:
        raise SMBError("download 需要 remote_path", "invalid_argument")
    local = _local_path(a, "local_path")
    trailing = str(a.get("local_path") or "").endswith((os.sep, "/", "\\"))
    kind = client.probe(remote)
    if kind == "missing":
        raise SMBError("远程路径不存在: %s" % remote, "not_found")
    if kind == "dir":
        target = local
        if os.path.exists(local) and not os.path.isdir(local):
            raise SMBError("本地目标已存在且不是目录: %s" % local, "invalid_argument")
        os.makedirs(target, exist_ok=True)
        commands = ["lcd " + _quote(target), "prompt OFF", "recurse ON"]
        if remote:
            commands.append("cd " + _quote(remote))
        commands.append("mget *")
        client.run("; ".join(commands))
        files, dirs, bytes_total = _walk_stats(target)
        return ok({"action": "download", "remote_path": remote, "local_path": target,
                   "files": files, "dirs": dirs, "bytes": bytes_total})
    target = local
    if os.path.isdir(local) or trailing:
        target = os.path.join(local, remote.rsplit("\\", 1)[-1])
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    client.run("get %s %s" % (_quote(remote), _quote(target)))
    size = os.path.getsize(target) if os.path.exists(target) else 0
    return ok({"action": "download", "remote_path": remote, "local_path": target,
               "files": 1, "dirs": 0, "bytes": size})


def _do_delete(client, a):
    remote = _normalize_remote(a.get("remote_path"))
    if not remote:
        raise SMBError("不允许删除共享根，请指定 remote_path", "invalid_argument")
    recursive = bool(a.get("recursive", False))
    kind = client.probe(remote)
    if kind == "missing":
        raise SMBError("远程路径不存在: %s" % remote, "not_found")
    if kind == "dir":
        files, dirs = client.delete_tree(remote, recursive)
        return ok({"action": "delete", "remote_path": remote, "deleted_files": files,
                   "deleted_dirs": dirs, "recursive": recursive})
    client.run("del " + _quote(remote))
    return ok({"action": "delete", "remote_path": remote, "deleted_files": 1,
               "deleted_dirs": 0, "recursive": recursive})


def _do_list(client, a):
    remote = _normalize_remote(a.get("remote_path"))
    kind = client.probe(remote) if remote else "dir"
    if kind == "missing":
        raise SMBError("远程路径不存在: %s" % remote, "not_found")
    if kind == "file":
        entries = client.list_entries(remote, is_dir=False)
        if not entries:
            entries = [{"name": remote.rsplit("\\", 1)[-1], "is_dir": False,
                        "size": 0, "attributes": ""}]
        return ok({"action": "list", "path": remote, "count": len(entries),
                   "entries": entries})
    entries = client.list_entries(remote)
    return ok({"action": "list", "path": remote, "count": len(entries), "entries": entries})


_ACTIONS = {
    "upload": _do_upload,
    "download": _do_download,
    "delete": _do_delete,
    "list": _do_list,
}


def run(arguments):
    a = arguments or {}
    _load_config()
    action = str(a.get("action") or "list").strip().lower()
    if action not in _ACTIONS:
        return error("未知 action '%s'，可用: %s" % (action, " / ".join(_ACTIONS)),
                     code="unknown_action")
    try:
        client = _connection(a)
        return _ACTIONS[action](client, a)
    except SMBError as exc:
        return error(str(exc), code=exc.code)
    except (OSError, ValueError) as exc:
        return error("本地 I/O 失败: %s" % exc, code="io_error")


if __name__ == "__main__":  # 本地调试：python skill.py '{"action":"list","host":"...","share":"...","username":"..."}'
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
