"""SSH / SCP 技能实现（paramiko）。

- exec：远端执行命令，返回 exit_status / stdout / stderr
- upload / download：SFTP 传输，支持文件与目录递归
- host_key：查看 / 删除已保存的主机公钥（known_hosts）
连接参数优先级：arguments > 环境变量 / 同目录 config.json。

主机公钥：默认 accept_new —— 首次连接认证成功后把主机公钥追加到 known_hosts
（默认 ~/.ssh/known_hosts，不可写时回退到技能目录 known_hosts），之后同主机再连即校验；
已记录但公钥变化会直接报 host_key_mismatch，不会静默覆盖。

内部统一使用 dict 信封：{"status":"ok","result":...} / {"status":"error","error":...,"code":...}；
对外由入口 skill.py 调 _emit() 转成运行时原生信封（shell-tool=JSON 字符串 / 移动端=dict）。
"""
import base64
import hashlib
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
DEFAULT_KNOWN_HOSTS = "~/.ssh/known_hosts"
# 未显式指定私钥时，按此顺序在 ~/.ssh 下查找默认私钥（用于带 passphrase 的场景）
DEFAULT_KEY_FILES = ("id_ed25519", "id_ecdsa", "id_rsa", "id_dsa")
# 主机公钥策略：accept_new（首次记录，默认）/ strict（只信任已记录）/ off（不校验，旧行为）
HOST_KEY_POLICIES = ("accept_new", "strict", "off")


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


def _known_hosts_file(args):
    """known_hosts 路径：arguments > SSH_KNOWN_HOSTS > ~/.ssh/known_hosts。"""
    path = args.get("known_hosts") or os.environ.get("SSH_KNOWN_HOSTS") or DEFAULT_KNOWN_HOSTS
    return os.path.normpath(os.path.expanduser(str(path).strip()))


def _resolve_host_key_policy(args):
    """主机公钥策略；兼容旧的 strict_host_key 布尔参数。"""
    raw = args.get("host_key_policy") or os.environ.get("SSH_HOST_KEY_POLICY")
    if raw:
        policy = str(raw).strip().lower()
        if policy not in HOST_KEY_POLICIES:
            raise SshError(
                "host_key_policy 仅支持 %s" % " / ".join(HOST_KEY_POLICIES), code="bad_policy"
            )
        return policy
    strict = _as_bool(args.get("strict_host_key", os.environ.get("SSH_STRICT_HOST_KEY")), False)
    return "strict" if strict else "accept_new"


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
        "host_key_policy": _resolve_host_key_policy(args),
        "known_hosts": _known_hosts_file(args),
        "timeout": _as_float(
            args.get("timeout", os.environ.get("SSH_TIMEOUT")), DEFAULT_TIMEOUT
        ),
    }


def _key_parse_error(last_error):
    text = str(last_error).lower()
    if "password" in text or "decrypt" in text:
        return SshError("passphrase 不正确（或私钥已损坏）", code="passphrase_invalid")
    return SshError("私钥解析失败: %s" % last_error, code="bad_key")


def _load_key_file(path, passphrase=None):
    """按密钥类型逐个尝试解析私钥文件（PEM / OpenSSH：Ed25519 / ECDSA / RSA）。"""
    key_classes = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
    if hasattr(paramiko, "DSSKey"):
        key_classes.append(paramiko.DSSKey)

    last_error = None
    for key_class in key_classes:
        try:
            return key_class.from_private_key_file(path, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise SshError("私钥已加密，请提供 passphrase", code="passphrase_required")
        except Exception as exc:  # 换下一种密钥类型
            last_error = exc
    raise _key_parse_error(last_error)


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

    if is_path:
        if hasattr(paramiko.PKey, "from_path"):
            try:
                return paramiko.PKey.from_path(path, passphrase=passphrase)
            except Exception:
                pass  # 回退到逐类解析（旧版 paramiko）
        return _load_key_file(path, passphrase)

    key_classes = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
    if hasattr(paramiko, "DSSKey"):
        key_classes.append(paramiko.DSSKey)

    last_error = None
    for key_class in key_classes:
        try:
            source.seek(0)
            return key_class.from_private_key(source, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise SshError("私钥已加密，请提供 passphrase", code="passphrase_required")
        except Exception as exc:  # 换下一种密钥类型
            last_error = exc
    raise _key_parse_error(last_error)


def _load_default_key(cfg):
    """未显式给私钥时，用 passphrase 尝试 ~/.ssh 下的默认私钥（带密码的 id_* 也能用）。"""
    passphrase = cfg["passphrase"] or None
    if not passphrase:
        return None  # 未给 passphrase 时交给 paramiko 的 look_for_keys / agent
    home = os.path.expanduser("~/.ssh")
    for name in DEFAULT_KEY_FILES:
        path = os.path.join(home, name)
        if not os.path.isfile(path):
            continue
        try:
            return _load_key_file(path, passphrase)
        except SshError as exc:
            if exc.code == "passphrase_invalid":
                raise  # passphrase 明确给了但不对，不要再静默回退
            continue
    return None


# ============================ 主机公钥 ============================
def _fingerprint(key):
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _fingerprint_from_blob(blob):
    try:
        raw = base64.b64decode(blob)
    except Exception:
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")


def _known_host_name(host, port):
    """known_hosts 里的主机名写法：非 22 端口用 [host]:port（与 OpenSSH / paramiko 一致）。"""
    return str(host) if int(port) == DEFAULT_PORT else "[%s]:%s" % (host, port)


def _known_hosts_candidates(path):
    """写入候选：配置路径优先，不可写（如移动端无 ~/.ssh 权限）时回退到技能目录。"""
    fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_hosts")
    candidates = [path]
    if os.path.abspath(path) != os.path.abspath(fallback):
        candidates.append(fallback)
    return candidates


def _write_lines(path, lines):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    prefix = ""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) not in (b"\n", b"\r"):
                prefix = "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(prefix + "\n".join(lines) + "\n")


def _append_host_keys(path, pending):
    """把首次连接拿到的主机公钥追加进 known_hosts；返回 (实际写入路径, 错误信息)。"""
    lines = ["%s %s %s" % (host, key.get_name(), key.get_base64()) for host, key in pending]
    last_error = None
    for target in _known_hosts_candidates(path):
        try:
            _write_lines(target, lines)
            return target, None
        except OSError as exc:
            last_error = exc
    return path, str(last_error)


class _RememberHostKeyPolicy(object):
    """首次连接先在内存记下主机公钥，认证成功后再落盘（避免把连不上的目标写进 known_hosts）。"""

    def __init__(self):
        self.pending = []

    def missing_host_key(self, client, hostname, key):
        self.pending.append((hostname, key))


def _load_known_hosts(client, path):
    try:
        client.load_system_host_keys()  # ~/.ssh/known_hosts 等系统记录
    except OSError:
        pass
    if os.path.isfile(path):
        try:
            client.load_host_keys(path)
        except (OSError, paramiko.SSHException):
            pass  # 文件损坏不阻断连接：本次按首次连接处理


def _host_key_report(client, cfg, pending, policy_name):
    """本次连接的主机公钥情况：saved（首次已保存）/ known / disabled / save_failed。"""
    report = {"status": "known", "known_hosts": cfg["known_hosts"]}
    transport = client.get_transport()
    if transport is not None:
        try:
            key = transport.get_remote_server_key()
            report["type"] = key.get_name()
            report["fingerprint"] = _fingerprint(key)
        except Exception:
            pass
    if policy_name == "off":
        report["status"] = "disabled"
        return report
    if pending:
        target, error = _append_host_keys(cfg["known_hosts"], pending)
        if error:
            report["status"] = "save_failed"
            report["error"] = error
        else:
            report["status"] = "saved"
            report["known_hosts"] = target
    return report


def _connect(cfg):
    if paramiko is None:
        raise SshError("缺少 paramiko，请先安装：pip install paramiko", code="missing_dependency")

    client = paramiko.SSHClient()
    policy_name = cfg["host_key_policy"]
    pending = []
    if policy_name == "off":
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        _load_known_hosts(client, cfg["known_hosts"])
        if policy_name == "strict":
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            remember = _RememberHostKeyPolicy()
            pending = remember.pending
            client.set_missing_host_key_policy(remember)

    kwargs = {
        "hostname": cfg["host"],
        "port": cfg["port"],
        "username": cfg["username"],
        "timeout": cfg["timeout"],
        "banner_timeout": cfg["timeout"],
        "auth_timeout": cfg["timeout"],
    }
    pkey = _load_pkey(cfg)
    if pkey is None and not cfg["password"]:
        pkey = _load_default_key(cfg)
    if pkey is not None:
        kwargs["pkey"] = pkey
    if cfg["password"]:
        kwargs["password"] = cfg["password"]
    if pkey is None and not cfg["password"]:
        kwargs["look_for_keys"] = True
        kwargs["allow_agent"] = True

    try:
        client.connect(**kwargs)
    except paramiko.BadHostKeyException as exc:
        client.close()
        raise SshError(
            "主机公钥与 known_hosts 记录不一致（可能存在中间人）：%s\n"
            "已记录: %s\n本次提供: %s\n确认无误后先删除旧记录再重连："
            '{"action": "host_key", "op": "remove", "host": "%s"}'
            % (cfg["host"], _fingerprint(exc.expected_key), _fingerprint(exc.key), cfg["host"]),
            code="host_key_mismatch",
        )
    except paramiko.AuthenticationException as exc:
        client.close()
        raise SshError("认证失败: %s" % exc, code="auth_failed")
    except (socket.timeout, OSError) as exc:
        client.close()
        raise SshError("连接 %s:%s 失败: %s" % (cfg["host"], cfg["port"], exc), code="connect_failed")
    except paramiko.SSHException as exc:
        client.close()
        if "not found in known_hosts" in str(exc):
            raise SshError(
                "主机公钥未记录且策略为 strict：%s\n首次连接请用 host_key_policy=accept_new 保存公钥"
                % cfg["host"],
                code="host_key_unknown",
            )
        raise SshError("SSH 握手失败: %s" % exc, code="ssh_failed")
    return client, _host_key_report(client, cfg, pending, policy_name)


# ============================ exec ============================
def _act_exec(args):
    cfg = _resolve(args)
    command = str(args.get("command") or "").strip()
    if not command:
        return _error("缺少 command 参数", code="missing_command")

    timeout = _as_float(args.get("timeout"), cfg["timeout"])
    client, host_key = _connect(cfg)
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
        "host_key": host_key,
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
    client, host_key = _connect(cfg)
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
        "host_key": host_key,
    })


def _act_download(args):
    remote_path = str(args.get("remote_path") or "").strip()
    local_path = os.path.expanduser(str(args.get("local_path") or "").strip())
    if not remote_path:
        return _error("缺少 remote_path 参数", code="missing_remote_path")
    if not local_path:
        return _error("缺少 local_path 参数", code="missing_local_path")

    cfg = _resolve(args)
    client, host_key = _connect(cfg)
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
        "host_key": host_key,
    })


# ============================ host_key ============================
def _read_known_hosts_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines(), None
    except OSError as exc:
        return [], str(exc)


def _write_all_lines(path, lines):
    """整体重写 known_hosts（先写临时文件再替换，避免写坏）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        if lines:
            handle.write("\n".join(lines) + "\n")
    os.replace(temp, path)


def _act_host_key(args):
    """已保存的主机公钥：op=list 列出 / op=remove 按主机删除。"""
    op = str(args.get("op") or "list").strip().lower()
    path = _known_hosts_file(args)

    if op == "list":
        lines, error = _read_known_hosts_lines(path)
        entries = []
        for line in lines:
            parts = line.split()
            if len(parts) < 3 or line.strip().startswith("#"):
                continue
            entries.append({
                "host": parts[0],
                "type": parts[1],
                "fingerprint": _fingerprint_from_blob(parts[2]),
            })
        result = {"op": op, "known_hosts": path, "count": len(entries), "entries": entries}
        if error:
            result["note"] = "文件不可读: %s" % error
        return _ok(result)

    if op == "remove":
        host = str(args.get("host") or "").strip()
        if not host:
            return _error("缺少 host 参数", code="missing_host")
        port = _as_int(args.get("port") or os.environ.get("SSH_PORT"), DEFAULT_PORT)
        target = _known_host_name(host, port)
        # 允许直接传 known_hosts 里的完整写法（[host]:port / host:port）
        names = {target}
        if ":" in host or host.startswith("["):
            names.add(host)
        lines, error = _read_known_hosts_lines(path)
        if error:
            return _error("known_hosts 不可读: %s" % error, code="known_hosts_unreadable")

        kept = []
        removed = 0
        hashed = False
        for line in lines:
            parts = line.split()
            if parts and not line.strip().startswith("#"):
                if parts[0].startswith("|"):
                    hashed = True
                elif names & set(parts[0].split(",")):
                    removed += 1
                    continue
            kept.append(line)
        if removed:
            _write_all_lines(path, kept)

        result = {"op": op, "known_hosts": path, "host": target, "removed": removed}
        if hashed and not removed:
            result["note"] = "文件中有哈希条目（|1|...），无法按主机名匹配，请用 ssh-keygen -R 删除"
        return _ok(result)

    return _error("未知 op '%s'，可用: list, remove" % op, code="unknown_op")


# ============================ 分发 ============================
ACTIONS = {
    "exec": _act_exec,
    "upload": _act_upload,
    "download": _act_download,
    "host_key": _act_host_key,
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
