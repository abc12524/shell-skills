"""SSH / SCP 技能实现（paramiko）。

- exec：远端执行命令，返回 exit_status / stdout / stderr
- upload / download：SFTP 传输，支持文件与目录递归
连接参数优先级：arguments > 环境变量 / 同目录 config.json。

内部统一使用 dict 信封：{"status":"ok","result":...} / {"status":"error","error":...,"code":...}；
对外由入口 skill.py 调 _emit() 转成运行时原生信封（shell-tool=JSON 字符串 / 移动端=dict）。
"""
import io
import json
import os
import posixpath
import socket
import stat

try:
    import paramiko
except ImportError:  # 延迟到调用时报错，便于返回友好提示
    paramiko = None

DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 30


# ============================ 信封兼容 ============================
def _builtin_ok(result=None):
    return json.dumps({"status": "ok", "result": {} if result is None else result},
                      ensure_ascii=False)


def _builtin_error(message, code=None, **extra):
    payload = {"status": "error", "error": str(message)}
    if code is not None:
        payload["code"] = code
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False)


try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py（ok/error 返回 dict）
    import _envelope as _native

    def _native_ok(result=None):
        return _native.ok(result if result is not None else {})

    def _native_error(message, code=None, **extra):
        return _native.error(message, code=code)  # 移动端信封不接受额外字段
except ImportError:
    try:  # shell-tool：core.tools.envelope（ok/error 返回 JSON 字符串）
        from core.tools.envelope import ok as _native_ok, error as _native_error
    except ImportError:  # 独立运行：内置信封
        _native_ok, _native_error = _builtin_ok, _builtin_error


# ============================ 内部信封 ============================
def _ok(result=None):
    return {"status": "ok", "result": {} if result is None else result}


def _error(message, code=None, **extra):
    payload = {"status": "error", "error": str(message)}
    if code is not None:
        payload["code"] = code
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def _emit(env):
    """把内部 dict 信封转成运行时信封：shell-tool/独立 → JSON 字符串，移动端 → dict。"""
    if not isinstance(env, dict):
        env = _ok(env)
    if env.get("status") == "ok":
        return _native_ok(env.get("result", {}))
    extra = {k: v for k, v in env.items() if k not in ("status", "error", "code")}
    return _native_error(env.get("error", ""), code=env.get("code"), **extra)


class SshError(Exception):
    """面向用户的 SSH 错误；code 为稳定错误码。"""

    def __init__(self, message, code="ssh_error"):
        super().__init__(message)
        self.code = code


# ============================ 配置 ============================
def _load_config():
    """把同目录 config.json 注入环境变量（不覆盖已有 env），照常 os.environ 读取。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(cfg, dict):
        for key, value in cfg.items():
            if key.startswith("_") or not value or os.environ.get(key):
                continue
            os.environ[key] = str(value)


_load_config()


def _as_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_int(value, default):
    if value is None or value == "":
        return int(default)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return int(default)


def _as_float(value, default):
    if value is None or value == "":
        return float(default)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return float(default)


def _resolve(args):
    """解析连接配置；arguments 覆盖环境变量 / config.json。"""
    host = str(args.get("host") or os.environ.get("SSH_HOST", "")).strip()
    username = str(args.get("username") or os.environ.get("SSH_USERNAME", "")).strip()
    if not host:
        raise SshError("缺少 host（或配置 SSH_HOST）", code="missing_host")
    if not username:
        raise SshError("缺少 username（或配置 SSH_USERNAME）", code="missing_username")

    return {
        "host": host,
        "port": _as_int(args.get("port") or os.environ.get("SSH_PORT"), DEFAULT_PORT),
        "username": username,
        "password": str(args.get("password") or os.environ.get("SSH_PASSWORD", "")),
        "private_key": str(args.get("private_key") or os.environ.get("SSH_PRIVATE_KEY", "")),
        "private_key_path": os.path.expanduser(str(
            args.get("private_key_path") or os.environ.get("SSH_PRIVATE_KEY_PATH", "")
        )),
        "passphrase": str(args.get("passphrase") or os.environ.get("SSH_PASSPHRASE", "")),
        "strict_host_key": _as_bool(
            args.get("strict_host_key", os.environ.get("SSH_STRICT_HOST_KEY")), False
        ),
        "timeout": _as_float(
            args.get("timeout", os.environ.get("SSH_TIMEOUT")), DEFAULT_TIMEOUT
        ),
    }


def _load_pkey(cfg):
    """加载私钥：支持 PEM / OpenSSH（Ed25519 / ECDSA / RSA）与 passphrase。"""
    text = cfg["private_key"]
    path = cfg["private_key_path"]
    if not text and not path:
        return None
    passphrase = cfg["passphrase"] or None

    if text and "-----BEGIN" in text and "PRIVATE KEY-----" in text:
        source = io.StringIO(text)
        is_path = False
    elif path:
        source = path
        is_path = True
    else:
        raise SshError("private_key 需为 PEM/OpenSSH 私钥文本", code="bad_key")

    if is_path and hasattr(paramiko.PKey, "from_path"):
        try:
            return paramiko.PKey.from_path(path, passphrase=passphrase)
        except Exception:
            pass  # 回退到逐类解析（旧版 paramiko）

    key_classes = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
    if hasattr(paramiko, "DSSKey"):
        key_classes.append(paramiko.DSSKey)

    last_error = None
    for key_class in key_classes:
        try:
            if is_path:
                return key_class.from_private_key_file(path, password=passphrase)
            source.seek(0)
            return key_class.from_private_key(source, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise SshError("私钥已加密，请提供 passphrase", code="passphrase_required")
        except Exception as exc:  # 换下一种密钥类型
            last_error = exc
    raise SshError("私钥解析失败: %s" % last_error, code="bad_key")


def _connect(cfg):
    if paramiko is None:
        raise SshError("缺少 paramiko，请先安装：pip install paramiko", code="missing_dependency")

    client = paramiko.SSHClient()
    if cfg["strict_host_key"]:
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs = {
        "hostname": cfg["host"],
        "port": cfg["port"],
        "username": cfg["username"],
        "timeout": cfg["timeout"],
        "banner_timeout": cfg["timeout"],
        "auth_timeout": cfg["timeout"],
    }
    pkey = _load_pkey(cfg)
    if pkey is not None:
        kwargs["pkey"] = pkey
    if cfg["password"]:
        kwargs["password"] = cfg["password"]
    if pkey is None and not cfg["password"]:
        kwargs["look_for_keys"] = True
        kwargs["allow_agent"] = True

    try:
        client.connect(**kwargs)
    except paramiko.AuthenticationException as exc:
        client.close()
        raise SshError("认证失败: %s" % exc, code="auth_failed")
    except (socket.timeout, OSError) as exc:
        client.close()
        raise SshError("连接 %s:%s 失败: %s" % (cfg["host"], cfg["port"], exc), code="connect_failed")
    except paramiko.SSHException as exc:
        client.close()
        raise SshError("SSH 握手失败: %s" % exc, code="ssh_failed")
    return client


# ============================ exec ============================
def _act_exec(args):
    cfg = _resolve(args)
    command = str(args.get("command") or "").strip()
    if not command:
        return _error("缺少 command 参数", code="missing_command")

    timeout = _as_float(args.get("timeout"), cfg["timeout"])
    client = _connect(cfg)
    try:
        stdin, stdout, stderr = client.exec_command(
            command, timeout=timeout, get_pty=_as_bool(args.get("pty"), False)
        )
        channel = stdout.channel
        channel.settimeout(timeout)
        try:
            out = stdout.read()
            err = stderr.read()
        except socket.timeout:
            channel.close()
            return _error("命令执行超时（%ss）" % timeout, code="timeout")
        exit_status = channel.recv_exit_status()
    finally:
        client.close()

    return _ok({
        "command": command,
        "exit_status": exit_status,
        "stdout": out.decode("utf-8", errors="replace"),
        "stderr": err.decode("utf-8", errors="replace"),
    })


# ============================ SFTP 辅助 ============================
def _sftp_makedirs(sftp, path):
    path = posixpath.normpath(path)
    if path in ("", ".", "/"):
        return
    missing = []
    current = path
    while current not in ("", "/", "."):
        try:
            sftp.stat(current)
            break
        except IOError:
            missing.append(current)
            current = posixpath.dirname(current)
    for directory in reversed(missing):
        try:
            sftp.mkdir(directory)
        except IOError:
            # 并发创建或权限不足：再确认一次，避免误报
            sftp.stat(directory)


def _sftp_walk(sftp, root):
    """递归产出 (所在目录, 文件路径)。"""
    entries = sftp.listdir_attr(root)
    subdirs = []
    for entry in entries:
        full = posixpath.join(root, entry.filename)
        if stat.S_ISDIR(entry.st_mode or 0):
            subdirs.append(full)
        else:
            yield root, full
    for subdir in subdirs:
        yield from _sftp_walk(sftp, subdir)


def _act_upload(args):
    local_path = os.path.expanduser(str(args.get("local_path") or "").strip())
    remote_path = str(args.get("remote_path") or "").strip()
    if not local_path:
        return _error("缺少 local_path 参数", code="missing_local_path")
    if not remote_path:
        return _error("缺少 remote_path 参数", code="missing_remote_path")
    if not os.path.exists(local_path):
        return _error("本地路径不存在: %s" % local_path, code="local_not_found")

    cfg = _resolve(args)
    client = _connect(cfg)
    files = 0
    total_bytes = 0
    try:
        sftp = client.open_sftp()
        try:
            if os.path.isdir(local_path):
                target_root = remote_path
                if remote_path.endswith("/"):
                    target_root = posixpath.join(remote_path, os.path.basename(os.path.normpath(local_path)))
                _sftp_makedirs(sftp, target_root)
                for dirpath, _dirnames, filenames in os.walk(local_path):
                    rel_dir = os.path.relpath(dirpath, local_path)
                    remote_dir = target_root if rel_dir == "." else posixpath.join(
                        target_root, rel_dir.replace(os.sep, "/")
                    )
                    _sftp_makedirs(sftp, remote_dir)
                    for filename in filenames:
                        source = os.path.join(dirpath, filename)
                        destination = posixpath.join(remote_dir, filename)
                        sftp.put(source, destination)
                        files += 1
                        total_bytes += os.path.getsize(source)
            else:
                destination = remote_path
                if remote_path.endswith("/"):
                    destination = posixpath.join(remote_path, os.path.basename(local_path))
                _sftp_makedirs(sftp, posixpath.dirname(destination))
                sftp.put(local_path, destination)
                files = 1
                total_bytes = os.path.getsize(local_path)
        finally:
            sftp.close()
    finally:
        client.close()

    return _ok({
        "direction": "upload",
        "local_path": local_path,
        "remote_path": remote_path,
        "files": files,
        "bytes": total_bytes,
    })


def _act_download(args):
    remote_path = str(args.get("remote_path") or "").strip()
    local_path = os.path.expanduser(str(args.get("local_path") or "").strip())
    if not remote_path:
        return _error("缺少 remote_path 参数", code="missing_remote_path")
    if not local_path:
        return _error("缺少 local_path 参数", code="missing_local_path")

    cfg = _resolve(args)
    client = _connect(cfg)
    files = 0
    total_bytes = 0
    try:
        sftp = client.open_sftp()
        try:
            try:
                remote_stat = sftp.stat(remote_path)
            except IOError:
                return _error("远端路径不存在: %s" % remote_path, code="remote_not_found")

            if stat.S_ISDIR(remote_stat.st_mode or 0):
                target_root = local_path
                if local_path.endswith(os.sep) or local_path.endswith("/") or os.path.isdir(local_path):
                    target_root = os.path.join(
                        local_path, posixpath.basename(posixpath.normpath(remote_path))
                    )
                os.makedirs(target_root, exist_ok=True)
                for remote_dir, remote_file in _sftp_walk(sftp, remote_path):
                    rel_dir = posixpath.relpath(remote_dir, remote_path)
                    local_dir = target_root if rel_dir == "." else os.path.join(
                        target_root, rel_dir.replace("/", os.sep)
                    )
                    os.makedirs(local_dir, exist_ok=True)
                    destination = os.path.join(local_dir, posixpath.basename(remote_file))
                    sftp.get(remote_file, destination)
                    files += 1
                    total_bytes += os.path.getsize(destination)
            else:
                destination = local_path
                if local_path.endswith(os.sep) or local_path.endswith("/") or os.path.isdir(local_path):
                    destination = os.path.join(
                        local_path, posixpath.basename(posixpath.normpath(remote_path))
                    )
                parent = os.path.dirname(destination)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                sftp.get(remote_path, destination)
                files = 1
                total_bytes = os.path.getsize(destination)
        finally:
            sftp.close()
    finally:
        client.close()

    return _ok({
        "direction": "download",
        "remote_path": remote_path,
        "local_path": local_path,
        "files": files,
        "bytes": total_bytes,
    })


# ============================ 分发 ============================
ACTIONS = {
    "exec": _act_exec,
    "upload": _act_upload,
    "download": _act_download,
}


def dispatch(arguments):
    args = arguments or {}
    action = str(args.get("action") or "exec").strip().lower()
    handler = ACTIONS.get(action)
    if handler is None:
        return _error(
            "未知 action '%s'，可用: %s" % (action, ", ".join(ACTIONS)),
            code="unknown_action",
        )
    try:
        return handler(args)
    except SshError as exc:
        return _error(str(exc), code=exc.code)
    except Exception as exc:  # 兜底：网络中断、权限等
        return _error("SSH 操作失败: %s" % exc, code="internal")
