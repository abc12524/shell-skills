# skill: ftp
# description: FTP/FTPS 文件传输（标准库 ftplib）：文件/文件夹上传、下载、删除、列表
# status: enable
# usage:
#   arguments.action = upload | download | delete | list
#     upload    上传文件/文件夹：local_path -> remote_path
#     download  下载文件/文件夹：remote_path -> local_path
#     delete    删除文件/文件夹：remote_path（删除非空目录需 recursive=true）
#     list      列出目录：remote_path（默认登录根目录）
#   连接参数（也可用同目录 config.json / 环境变量 FTP_* 提供）：
#     arguments.host     主机名或 IP（必填）
#     arguments.port     端口（默认 21）
#     arguments.username 用户名（默认 anonymous）
#     arguments.password 密码（默认 anonymous@）
#     arguments.secure   true 使用 FTPS 显式 TLS（AUTH TLS + PBSZ/PROT），默认 false
#     arguments.timeout  连接/读写超时秒数（默认 30）
#     arguments.passive  true 被动模式（默认 true），false 为主动模式
#     arguments.encoding 控制连接编码，默认 utf-8；中文文件名乱码的服务端可设 gbk
#                        （解码失败时按替换字符处理，不会中断列举）
#   路径参数：
#     arguments.remote_path 服务器路径，用 / 分隔，如 "dir/file.txt"
#     arguments.local_path  本机路径（绝对或相对当前目录）
#     arguments.recursive   删除目录时是否递归（默认 false；upload/download 目录恒递归）
#   说明: 纯标准库实现（ftplib），无需第三方依赖；已适配常见 FTP 服务端与 vsftpd
#         （MLSD 列举 + LIST 解析回退，PASV 被动模式，路径用 / 分隔）。
#         FTPS 为显式 TLS（AUTH TLS，端口通常 21），不支持隐式 TLS（端口 990）；
#         对 vsftpd 的 FTPS 如遇会话复用报错，可在服务端设置 require_ssl_reuse=NO。
#   示例: {"action": "list", "host": "192.168.1.10", "username": "u", "password": "p"}
#         {"action": "upload", "local_path": "./a.txt", "remote_path": "docs/a.txt", ...}
#         {"action": "download", "remote_path": "docs", "local_path": "./dst", ...}
#         {"action": "delete", "remote_path": "docs", "recursive": true, ...}
import ftplib
import json
import os
import re
import sys

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

DEFAULT_TIMEOUT = 30
_UNIX_LIST_RE = re.compile(
    r"^([\-ld])([rwxsStT\-]{9})\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+\d+\s+"
    r"(?:\d{2}:\d{2}|\d{4})\s+(.*)$")
_WIN_LIST_RE = re.compile(
    r"^\d{2}-\d{2}-\d{2,4}\s+\d{2}:\d{2}(?:AM|PM)\s+(<DIR>|\d+)\s+(.*)$", re.IGNORECASE)


class FTPError(Exception):
    def __init__(self, message, code="ftp_error"):
        super().__init__(message)
        self.code = code


class _TolerantControl:
    def _reopen_control(self):
        if self.sock is not None:
            self.file = self.sock.makefile("r", encoding=self.encoding, errors="replace",
                                           newline="\r\n")

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        result = super().connect(host, port, timeout, source_address)
        self._reopen_control()
        return result

    def login(self, *args, **kwargs):
        result = super().login(*args, **kwargs)
        self._reopen_control()
        return result


class _FTP(_TolerantControl, ftplib.FTP):
    pass


class _FTP_TLS(_TolerantControl, ftplib.FTP_TLS):
    pass


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


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _normalize_remote(value):
    text = str(value or "").replace("\\", "/").strip()
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise FTPError("remote_path 不允许包含 '..'", "invalid_argument")
        parts.append(part)
    return "/".join(parts)


def _remote_join(parent, name):
    if not parent:
        return name
    return parent + "/" + name


def _remote_split(path):
    if "/" in path:
        parent, name = path.rsplit("/", 1)
        return parent, name
    return "", path


def _safe_name(name):
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        raise FTPError("服务器返回了非法文件名: %r" % name, "bad_response")
    return name


class FtpSession:
    def __init__(self, host, port, username, password, secure=False, timeout=DEFAULT_TIMEOUT,
                 passive=True, encoding="utf-8"):
        self.host = host
        self.port = int(port)
        self.username = username or "anonymous"
        self.password = password if password else "anonymous@"
        self.secure = bool(secure)
        self.timeout = float(timeout)
        self.passive = bool(passive)
        self.encoding = encoding or "utf-8"
        self.ftp = None

    def connect(self):
        factory = _FTP_TLS if self.secure else _FTP
        ftp = factory()
        ftp.encoding = self.encoding
        ftp.connect(self.host, self.port, timeout=self.timeout)
        try:
            ftp.sendcmd("OPTS UTF8 ON")
        except ftplib.all_errors:
            pass
        ftp.login(self.username, self.password)
        if self.secure:
            ftp.prot_p()
        ftp.set_pasv(self.passive)
        self.ftp = ftp

    def close(self):
        if self.ftp is None:
            return
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass
        finally:
            self.ftp = None

    def entries(self, path):
        try:
            listing = []
            for name, facts in self.ftp.mlsd(path or None):
                if name in (".", ".."):
                    continue
                kind = facts.get("type", "")
                size = facts.get("size", "")
                listing.append({
                    "name": name,
                    "is_dir": kind in ("dir", "cdir", "pdir"),
                    "size": int(size) if size.isdigit() else 0,
                })
            return listing
        except (ftplib.error_perm, ftplib.error_proto, ftplib.error_temp):
            pass
        lines = []
        self.ftp.retrlines(("LIST " + path).strip() if path else "LIST", lines.append)
        return self._parse_list(lines)

    @staticmethod
    def _parse_list(lines):
        listing = []
        for line in lines:
            match = _UNIX_LIST_RE.match(line)
            if match:
                name = match.group(4)
                if " -> " in name:
                    name = name.split(" -> ", 1)[0]
                if name in (".", "..") or name.endswith(" ."):
                    continue
                listing.append({"name": name, "is_dir": match.group(1) == "d",
                                "size": int(match.group(3))})
                continue
            match = _WIN_LIST_RE.match(line)
            if match:
                raw_size = match.group(1)
                name = match.group(2)
                if name in (".", ".."):
                    continue
                listing.append({"name": name, "is_dir": raw_size.upper() == "<DIR>",
                                "size": 0 if raw_size.upper() == "<DIR>" else int(raw_size)})
        return listing

    def stat(self, path):
        if not path:
            return True, 0
        parent, name = _remote_split(path)
        for entry in self.entries(parent):
            if entry["name"] == name:
                return entry["is_dir"], entry["size"]
        raise FTPError("路径不存在: %s" % path, "not_found")

    def mkdir_p(self, path):
        if not path:
            return
        prefix = ""
        for part in path.split("/"):
            if not part:
                continue
            prefix = part if not prefix else prefix + "/" + part
            try:
                self.ftp.mkd(prefix)
            except ftplib.all_errors:
                pass

    def upload_file(self, local_path, remote_path):
        self.mkdir_p(_remote_split(remote_path)[0])
        with open(local_path, "rb") as source:
            self.ftp.storbinary("STOR " + remote_path, source, blocksize=1024 * 1024)

    def download_file(self, remote_path, local_path):
        temp_path = local_path + ".ftppart"
        with open(temp_path, "wb") as target:
            self.ftp.retrbinary("RETR " + remote_path, target.write, blocksize=1024 * 1024)
        os.replace(temp_path, local_path)

    def remove_file(self, remote_path):
        self.ftp.delete(remote_path)

    def remove_dir(self, remote_path):
        self.ftp.rmd(remote_path)

    def delete_tree(self, remote_path, recursive):
        entries = self.entries(remote_path)
        if entries and not recursive:
            raise FTPError("目录非空，需 recursive=true: %s" % remote_path, "directory_not_empty")
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


def _describe(exc):
    if isinstance(exc, FTPError):
        return str(exc), exc.code
    text = str(exc)
    upper = text.upper()
    if isinstance(exc, ftplib.error_perm):
        if "530" in text:
            return "登录失败（用户名或密码错误）: %s" % text, "logon_failure"
        if "NOT EMPTY" in upper:
            return "目录非空: %s" % text, "directory_not_empty"
        if "PERMISSION" in upper or "ACCESS" in upper or "DENIED" in upper:
            return "访问被拒绝: %s" % text, "access_denied"
        if "EXIST" in upper:
            return "目标已存在: %s" % text, "already_exists"
        if "NO SUCH" in upper or "NOT FOUND" in upper or "UNAVAILABLE" in upper:
            return "路径不存在: %s" % text, "not_found"
        return "FTP 永久错误: %s" % text, "ftp_error"
    if isinstance(exc, ftplib.error_temp):
        return "FTP 临时错误: %s" % text, "ftp_temp_error"
    if isinstance(exc, (ftplib.error_proto, EOFError)):
        return "FTP 协议错误: %s" % text, "protocol_error"
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "连接失败: %s" % text, "connect_failed"
    if isinstance(exc, OSError):
        return "网络/IO 错误: %s" % text, "io_error"
    return "%s: %s" % (type(exc).__name__, text), "ftp_error"


def _connection(a):
    host = str(a.get("host") or os.environ.get("FTP_HOST") or "").strip()
    port = a.get("port") or os.environ.get("FTP_PORT") or 21
    username = a.get("username")
    if username is None:
        username = os.environ.get("FTP_USERNAME", "anonymous")
    password = a.get("password")
    if password is None:
        password = os.environ.get("FTP_PASSWORD", "")
    secure = a.get("secure")
    if secure is None:
        secure = os.environ.get("FTP_SECURE")
    passive = a.get("passive")
    timeout = a.get("timeout") or DEFAULT_TIMEOUT
    encoding = a.get("encoding") or os.environ.get("FTP_ENCODING") or "utf-8"
    if not host:
        raise FTPError("缺少 host（或环境变量 FTP_HOST）", "invalid_argument")
    try:
        port = int(port)
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise FTPError("port/timeout 必须是数字", "invalid_argument")
    return FtpSession(host, port, username, str(password), _as_bool(secure),
                      timeout, _as_bool(passive, True), str(encoding))


def _local_path(a, key):
    raw = a.get(key)
    if raw is None or str(raw).strip() == "":
        raise FTPError("缺少 %s" % key, "invalid_argument")
    return os.path.abspath(os.path.expanduser(os.fspath(raw)))


def _upload_tree(session, local_root, remote_root, stats):
    session.mkdir_p(remote_root)
    stats["dirs"] += 1
    for dirpath, _, filenames in os.walk(local_root):
        rel = os.path.relpath(dirpath, local_root)
        remote_dir = remote_root
        if rel not in (".", ""):
            remote_dir = _remote_join(remote_root, rel.replace(os.sep, "/"))
            session.mkdir_p(remote_dir)
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
        raise FTPError("本地路径不存在: %s" % local, "not_found")
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
        raise FTPError("download 需要 remote_path", "invalid_argument")
    local = _local_path(a, "local_path")
    trailing = str(a.get("local_path") or "").endswith((os.sep, "/", "\\"))
    is_dir, _ = session.stat(remote)
    if is_dir:
        if os.path.exists(local) and not os.path.isdir(local):
            raise FTPError("本地目标已存在且不是目录: %s" % local, "invalid_argument")
        stats = {"files": 0, "dirs": 0, "bytes": 0}
        _download_tree(session, remote, local, stats)
        return ok({"action": "download", "remote_path": remote, "local_path": local,
                   "files": stats["files"], "dirs": stats["dirs"], "bytes": stats["bytes"]})
    target = local
    if os.path.isdir(local) or trailing:
        target = os.path.join(local, remote.rsplit("/", 1)[-1])
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    session.download_file(remote, target)
    return ok({"action": "download", "remote_path": remote, "local_path": target,
               "files": 1, "dirs": 0, "bytes": os.path.getsize(target)})


def _do_delete(session, a):
    remote = _normalize_remote(a.get("remote_path"))
    if not remote:
        raise FTPError("不允许删除登录根目录，请指定 remote_path", "invalid_argument")
    recursive = _as_bool(a.get("recursive"), False)
    is_dir, _ = session.stat(remote)
    if is_dir:
        files, dirs = session.delete_tree(remote, recursive)
        return ok({"action": "delete", "remote_path": remote, "deleted_files": files,
                   "deleted_dirs": dirs, "recursive": recursive})
    session.remove_file(remote)
    return ok({"action": "delete", "remote_path": remote, "deleted_files": 1,
               "deleted_dirs": 0, "recursive": recursive})


def _do_list(session, a):
    remote = _normalize_remote(a.get("remote_path"))
    if remote:
        is_dir, size = session.stat(remote)
        if not is_dir:
            return ok({"action": "list", "path": remote, "count": 1,
                       "entries": [{"name": remote.rsplit("/", 1)[-1], "is_dir": False,
                                    "size": size}]})
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
    action = str(a.get("action") or "list").strip().lower()
    if action not in _ACTIONS:
        return error("未知 action '%s'，可用: %s" % (action, " / ".join(_ACTIONS)),
                     code="unknown_action")
    session = None
    try:
        session = _connection(a)
        session.connect()
        return _ACTIONS[action](session, a)
    except Exception as exc:
        message, code = _describe(exc)
        return error(message, code=code)
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":  # 本地调试：python skill.py '{"action":"list","host":"...","username":"..."}'
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
