# skill: smb
# description: SMB/CIFS 文件传输（纯 Python smbprotocol）：文件/文件夹上传、下载、删除、列表
# status: enable
# usage:
#   arguments.action = upload | download | delete | list（默认 list）
#     upload / download 支持文件夹递归；delete 非空目录需 recursive=true
#   arguments.host / share / username(可 domain\\user) / password / domain / port(445) / timeout(60)
#   arguments.remote_path（共享内）/ local_path
#   配置: config.json 的 SMB_*；依赖 smbprotocol，无需系统 smbclient，支持 SMB 2.x/3.x
#   示例: {"action":"list","host":"192.168.1.10","share":"data","username":"u","password":"p"}
#         {"action":"upload","local_path":"./a.txt","remote_path":"docs/a.txt", ...}
import json
import os
import shutil
import stat as stat_module
import sys

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

try:
    import logging
    import smbclient
    logging.getLogger("smbprotocol").setLevel(logging.CRITICAL)
    logging.getLogger("smbclient").setLevel(logging.CRITICAL)
    _SMB_IMPORT_ERROR = None
except ImportError as exc:  # 缺失依赖时在 run() 里返回可读错误
    smbclient = None
    _SMB_IMPORT_ERROR = str(exc)

DEFAULT_TIMEOUT = 60

_ERROR_BY_NAME = {
    "LogonFailure": ("登录失败（用户名或密码错误）", "logon_failure"),
    "SMBAuthenticationError": ("认证失败（用户名/密码/域或 NTLM 未启用）", "logon_failure"),
    "AccessDenied": ("访问被拒绝（权限不足）", "access_denied"),
    "ObjectNameNotFound": ("路径不存在", "not_found"),
    "ObjectPathNotFound": ("上级路径不存在", "not_found"),
    "NoSuchFile": ("文件不存在", "not_found"),
    "BadNetworkName": ("共享不存在或不可访问", "bad_network_name"),
    "DirectoryNotEmpty": ("目录非空", "directory_not_empty"),
    "FileIsADirectory": ("目标是目录", "is_directory"),
    "NotADirectory": ("目标不是目录", "not_a_directory"),
    "CannotDelete": ("无法删除（可能被占用或只读）", "cannot_delete"),
    "FileExists": ("目标已存在", "already_exists"),
    "ObjectNameCollision": ("名称冲突（目标已存在）", "already_exists"),
    "SharingViolation": ("文件被占用（共享冲突）", "sharing_violation"),
}

_ERRNO_CODE = {
    2: "not_found",
    13: "access_denied",
    17: "already_exists",
    20: "not_a_directory",
    21: "is_directory",
    39: "directory_not_empty",
}

_NTSTATUS_CODE = {
    0xC0000034: ("路径不存在", "not_found"),
    0xC000003A: ("上级路径不存在", "not_found"),
    0xC0000225: ("共享或路径不存在", "bad_network_name"),
    0xC0000022: ("访问被拒绝（权限不足）", "access_denied"),
    0xC000006D: ("登录失败（用户名或密码错误）", "logon_failure"),
    0xC00000CC: ("共享不存在或不可访问", "bad_network_name"),
    0xC0000101: ("目录非空", "directory_not_empty"),
    0xC00000BA: ("目标是目录", "is_directory"),
    0xC0000103: ("目标不是目录", "not_a_directory"),
    0xC0000043: ("文件被占用（共享冲突）", "sharing_violation"),
    0xC0000035: ("名称冲突（目标已存在）", "already_exists"),
    0xC0000121: ("无法删除（可能被占用或只读）", "cannot_delete"),
}


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


def _safe_name(name):
    if name in ("", ".", "..") or "\\" in name or "/" in name:
        raise SMBError("服务器返回了非法文件名: %r" % name, "bad_response")
    return name


def _describe(exc):
    name = type(exc).__name__
    if name in _ERROR_BY_NAME:
        return _ERROR_BY_NAME[name]
    status = getattr(exc, "ntstatus", None)
    if status is not None:
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
    if status in _NTSTATUS_CODE:
        return _NTSTATUS_CODE[status]
    errno = getattr(exc, "errno", None)
    if errno in _ERRNO_CODE:
        return str(exc), _ERRNO_CODE[errno]
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "连接失败: %s" % exc, "connect_failed"
    return "%s: %s" % (name, exc), "smb_error"


class SmbSession:
    def __init__(self, host, share, username, password, domain="", port=445, timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.share = share.strip("\\/")
        self.username, self.domain = _split_account(username, domain)
        self.password = password or ""
        self.port = int(port)
        self.timeout = float(timeout)
        self.root = "\\\\%s\\%s" % (self.host, self.share)

    def _kwargs(self):
        account = self.username
        if account and self.domain:
            account = self.domain + "\\" + account
        return {
            "username": account,
            "password": self.password,
            "port": self.port,
            "connection_timeout": self.timeout,
        }

    def path(self, remote):
        return self.root + ("\\" + remote if remote else "")

    def connect(self):
        smbclient.register_session(self.host, **self._kwargs())

    def stat(self, remote):
        return smbclient.stat(self.path(remote), follow_symlinks=False, **self._kwargs())

    def makedirs(self, remote):
        if remote:
            smbclient.makedirs(self.path(remote), exist_ok=True, **self._kwargs())

    def entries(self, remote):
        result = []
        with smbclient.scandir(self.path(remote), **self._kwargs()) as iterator:
            for entry in iterator:
                info = entry.stat(follow_symlinks=False)
                result.append({
                    "name": entry.name,
                    "is_dir": stat_module.S_ISDIR(info.st_mode),
                    "size": info.st_size,
                })
        return result

    def upload_file(self, local_path, remote_path):
        parent = remote_path.rsplit("\\", 1)[0] if "\\" in remote_path else ""
        self.makedirs(parent)
        with open(local_path, "rb") as source:
            with smbclient.open_file(self.path(remote_path), mode="wb", **self._kwargs()) as target:
                shutil.copyfileobj(source, target, 1024 * 1024)

    def download_file(self, remote_path, local_path):
        temp_path = local_path + ".smbpart"
        with smbclient.open_file(self.path(remote_path), mode="rb", **self._kwargs()) as source:
            with open(temp_path, "wb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
        os.replace(temp_path, local_path)

    def remove_file(self, remote_path):
        smbclient.remove(self.path(remote_path), **self._kwargs())

    def remove_dir(self, remote_path):
        smbclient.rmdir(self.path(remote_path), **self._kwargs())

    def delete_tree(self, remote_path, recursive):
        entries = self.entries(remote_path)
        if entries and not recursive:
            raise SMBError("目录非空，需 recursive=true: %s" % remote_path, "directory_not_empty")
        files = 0
        dirs = 0
        for entry in entries:
            child = _remote_join(remote_path, _safe_name(entry["name"]))
            if entry["is_dir"]:
                sub_files, sub_dirs = self.delete_tree(child, recursive)
                files += sub_files
                dirs += sub_dirs
            else:
                self.remove_file(child)
                files += 1
        self.remove_dir(remote_path)
        return files, dirs + 1


def _connection(a):
    host = str(a.get("host") or os.environ.get("SMB_HOST") or "").strip()
    share = str(a.get("share") or os.environ.get("SMB_SHARE") or "").strip()
    username = a.get("username") or os.environ.get("SMB_USERNAME") or ""
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
    return SmbSession(host, share, username, str(password), domain, port, timeout)


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


def _upload_tree(session, local_root, remote_root, stats):
    session.makedirs(remote_root)
    stats["dirs"] += 1
    for dirpath, _, filenames in os.walk(local_root):
        rel = os.path.relpath(dirpath, local_root)
        remote_dir = remote_root
        if rel not in (".", ""):
            remote_dir = _remote_join(remote_root, rel.replace(os.sep, "\\"))
            session.makedirs(remote_dir)
            stats["dirs"] += 1
        for name in filenames:
            local_file = os.path.join(dirpath, name)
            session.upload_file(local_file, _remote_join(remote_dir, name))
            stats["files"] += 1
            try:
                stats["bytes"] += os.path.getsize(local_file)
            except OSError:
                pass


def _download_tree(session, remote_root, local_root, stats):
    os.makedirs(local_root, exist_ok=True)
    for entry in session.entries(remote_root):
        name = _safe_name(entry["name"])
        local_child = os.path.join(local_root, name)
        remote_child = _remote_join(remote_root, name)
        if entry["is_dir"]:
            _download_tree(session, remote_child, local_child, stats)
            stats["dirs"] += 1
        else:
            session.download_file(remote_child, local_child)
            stats["files"] += 1
            stats["bytes"] += entry["size"]


def _do_upload(session, a):
    local = _local_path(a, "local_path")
    if not os.path.exists(local):
        raise SMBError("本地路径不存在: %s" % local, "not_found")
    remote = _normalize_remote(a.get("remote_path"))
    trailing = str(a.get("remote_path") or "").endswith(("/", "\\"))
    stats = {"files": 0, "dirs": 0, "bytes": 0}
    if os.path.isdir(local):
        if not remote or trailing:
            remote = _remote_join(remote, os.path.basename(local.rstrip(os.sep)))
        _upload_tree(session, local, remote, stats)
    else:
        if not remote or trailing:
            remote = _remote_join(remote, os.path.basename(local))
        session.upload_file(local, remote)
        stats["files"] = 1
        stats["bytes"] = os.path.getsize(local)
    return ok({"action": "upload", "remote_path": remote, "local_path": local,
               "files": stats["files"], "dirs": stats["dirs"], "bytes": stats["bytes"]})


def _do_download(session, a):
    remote = _normalize_remote(a.get("remote_path"))
    if not remote:
        raise SMBError("download 需要 remote_path", "invalid_argument")
    local = _local_path(a, "local_path")
    trailing = str(a.get("local_path") or "").endswith((os.sep, "/", "\\"))
    info = session.stat(remote)
    if stat_module.S_ISDIR(info.st_mode):
        if os.path.exists(local) and not os.path.isdir(local):
            raise SMBError("本地目标已存在且不是目录: %s" % local, "invalid_argument")
        stats = {"files": 0, "dirs": 0, "bytes": 0}
        _download_tree(session, remote, local, stats)
        return ok({"action": "download", "remote_path": remote, "local_path": local,
                   "files": stats["files"], "dirs": stats["dirs"], "bytes": stats["bytes"]})
    target = local
    if os.path.isdir(local) or trailing:
        target = os.path.join(local, remote.rsplit("\\", 1)[-1])
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    session.download_file(remote, target)
    return ok({"action": "download", "remote_path": remote, "local_path": target,
               "files": 1, "dirs": 0, "bytes": os.path.getsize(target)})


def _do_delete(session, a):
    remote = _normalize_remote(a.get("remote_path"))
    if not remote:
        raise SMBError("不允许删除共享根，请指定 remote_path", "invalid_argument")
    recursive = bool(a.get("recursive", False))
    info = session.stat(remote)
    if stat_module.S_ISDIR(info.st_mode):
        files, dirs = session.delete_tree(remote, recursive)
        return ok({"action": "delete", "remote_path": remote, "deleted_files": files,
                   "deleted_dirs": dirs, "recursive": recursive})
    session.remove_file(remote)
    return ok({"action": "delete", "remote_path": remote, "deleted_files": 1,
               "deleted_dirs": 0, "recursive": recursive})


def _do_list(session, a):
    remote = _normalize_remote(a.get("remote_path"))
    if remote:
        info = session.stat(remote)
        if not stat_module.S_ISDIR(info.st_mode):
            return ok({"action": "list", "path": remote, "count": 1,
                       "entries": [{"name": remote.rsplit("\\", 1)[-1], "is_dir": False,
                                    "size": info.st_size}]})
    entries = session.entries(remote)
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
    if _SMB_IMPORT_ERROR is not None:
        return error("缺少依赖 smbprotocol，请先安装：pip install smbprotocol（%s）" % _SMB_IMPORT_ERROR,
                     code="not_installed")
    action = str(a.get("action") or "list").strip().lower()
    if action not in _ACTIONS:
        return error("未知 action '%s'，可用: %s" % (action, " / ".join(_ACTIONS)),
                     code="unknown_action")
    try:
        session = _connection(a)
        session.connect()
        return _ACTIONS[action](session, a)
    except SMBError as exc:
        return error(str(exc), code=exc.code)
    except Exception as exc:
        message, code = _describe(exc)
        return error(message, code=code)


if __name__ == "__main__":  # 本地调试：python skill.py '{"action":"list","host":"...","share":"...","username":"..."}'
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
