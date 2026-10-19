#!/usr/bin/env python3
"""对象存储工具实现（S3 兼容，AWS Signature V4）。

参照 android-agent 内置工具 file_tool（ObjectStorageTool.kt）的功能与签名逻辑改写为纯
Python：upload / download / delete / delete_batch / list / copy / head / list_buckets /
create_bucket / delete_bucket / presign / create_folder / delete_folder。

连接配置来自同目录 config.json（键为环境变量名，模板见 config.example.json）或既有环境变量：
  S3_ENDPOINT_URL  服务地址，如 https://s3.example.com（AWS S3 / MinIO / Ceph / OSS 等）
  S3_ACCESS_KEY    访问密钥 ID
  S3_SECRET_KEY    访问密钥 Secret
  S3_REGION        区域（可选，默认按 host 自动推断；非 AWS 默认 us-east-1）

内部统一使用 dict 信封：{"status":"ok","result":...} / {"status":"error","error":...,"code":...}；
对外由入口 skill.py 调 _emit() 转成运行时原生信封（shell-tool=JSON 字符串 / 移动端=dict）。
"""
import hashlib
import hmac
import json
import mimetypes
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from xml.sax.saxutils import escape as _xml_escape

import requests

DEFAULT_REGION = "us-east-1"
DEFAULT_TIMEOUT = 60
MAX_KEYS_DEFAULT = 100
BATCH_SIZE = 1000
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
)


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


def _is_error(value):
    return isinstance(value, dict) and value.get("status") == "error"


def _emit(env):
    """把内部 dict 信封转成运行时信封：shell-tool/独立 → JSON 字符串，移动端 → dict。"""
    if not isinstance(env, dict):
        env = _ok(env)
    if env.get("status") == "ok":
        return _native_ok(env.get("result", {}))
    extra = {k: v for k, v in env.items() if k not in ("status", "error", "code")}
    return _native_error(env.get("error", ""), code=env.get("code"), **extra)


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
            if value and not os.environ.get(key):
                os.environ[key] = str(value)


_load_config()


class ObjStoreError(Exception):
    """面向用户的对象存储错误；code 为稳定错误码。"""

    def __init__(self, message, code="objstore_error"):
        super().__init__(message)
        self.code = code


# ============================ 连接参数 ============================
def _extract_region(endpoint, override=""):
    if override:
        return override
    host = urlparse(endpoint).hostname or ""
    if "amazonaws.com" in host:
        parts = host.split(".")
        if len(parts) >= 3:
            return parts[1]
    return DEFAULT_REGION


def _conn(args):
    """解析连接参数：arguments 覆盖 > config.json/env。返回 (endpoint, region, ak, sk)。"""
    endpoint = str(args.get("endpoint_url") or os.environ.get("S3_ENDPOINT_URL", "")).strip()
    if not endpoint:
        raise ObjStoreError("未配置对象存储服务地址（S3_ENDPOINT_URL），请配置同目录 config.json",
                            code="not_configured")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    endpoint = endpoint.rstrip("/")

    ak = str(args.get("access_key") or os.environ.get("S3_ACCESS_KEY", "")).strip()
    sk = str(args.get("secret_key") or os.environ.get("S3_SECRET_KEY", "")).strip()
    if not ak or not sk:
        raise ObjStoreError("缺少对象存储凭证（S3_ACCESS_KEY / S3_SECRET_KEY），请配置同目录 config.json",
                            code="not_configured")

    region_override = str(args.get("region") or os.environ.get("S3_REGION", "")).strip()
    region = _extract_region(endpoint, region_override)
    return endpoint, region, ak, sk


def _timeout(args):
    try:
        value = int(args.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        raise ObjStoreError("arguments.timeout 必须是整数", code="bad_request")
    return value if value > 0 else DEFAULT_TIMEOUT


# ============================ AWS Signature V4 ============================
def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def _canonical_encode(value):
    """RFC3986 百分号编码（unreserved = A-Za-z0-9-_.~），用于签名规范化。"""
    out = []
    for byte in str(value).encode("utf-8"):
        ch = chr(byte)
        out.append(ch if ch in _UNRESERVED else "%%%02X" % byte)
    return "".join(out)


def _canonical_path(path):
    if not path.startswith("/"):
        path = "/" + path
    return "/".join(_canonical_encode(seg) for seg in path.split("/"))


def _canonical_query(params):
    items = sorted((str(k), str(v)) for k, v in params.items())
    return "&".join("%s=%s" % (_canonical_encode(k), _canonical_encode(v)) for k, v in items)


def _host_of(endpoint):
    url = urlparse(endpoint)
    host = url.hostname or ""
    port = url.port
    if port and port not in (80, 443):
        return "%s:%d" % (host, port)
    return host


def _signing_key(secret_key, date_stamp, region):
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp.encode("utf-8"))
    k_region = _hmac_sha256(k_date, region.encode("utf-8"))
    k_service = _hmac_sha256(k_region, b"s3")
    return _hmac_sha256(k_service, b"aws4_request")


def _sign(method, endpoint, path, query_params, region, ak, sk, headers, payload_hash, now=None):
    """生成签名头（含 Authorization），返回 (headers, canonical_path, canonical_query)。"""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = _host_of(endpoint)

    signed = dict(headers)
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_hash

    lower_keys = sorted(k.lower() for k in signed)
    original = {k.lower(): k for k in signed}
    signed_headers = ";".join(lower_keys)
    canonical_headers = "".join(
        "%s:%s\n" % (lk, str(signed[original[lk]]).strip()) for lk in lower_keys
    )

    canon_path = _canonical_path(path)
    canon_query = _canonical_query(query_params)

    canonical_request = "\n".join(
        [method, canon_path, canon_query, canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = "%s/%s/s3/aws4_request" % (date_stamp, region)
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])
    signature = hmac.new(
        _signing_key(sk, date_stamp, region), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    signed["Authorization"] = (
        "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (ak, credential_scope, signed_headers, signature)
    )
    return signed, canon_path, canon_query


def _request(endpoint, region, ak, sk, method, path, *, query=None, headers=None,
             data=None, data_stream=None, payload_hash=None, content_type=None,
             timeout=DEFAULT_TIMEOUT, stream=False):
    """签名并发送一次 S3 请求，返回 requests.Response。"""
    query = dict(query or {})
    headers = dict(headers or {})
    if content_type:
        headers["Content-Type"] = content_type

    if data is None and data_stream is None:
        payload_hash = _EMPTY_SHA256
    elif payload_hash is None:
        payload_hash = _sha256_hex(data if data is not None else b"")

    signed, canon_path, canon_query = _sign(
        method, endpoint, path, query, region, ak, sk, headers, payload_hash
    )
    send_headers = {k: v for k, v in signed.items() if k.lower() != "host"}
    url = endpoint + canon_path + (("?" + canon_query) if canon_query else "")
    body = data_stream if data_stream is not None else data
    return requests.request(method, url, headers=send_headers, data=body,
                            timeout=timeout, stream=stream)


# ============================ XML 解析 ============================
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _findall(el, name):
    return [child for child in el if _local(child.tag) == name]


def _text(el, name):
    child = _find(el, name)
    return child.text or "" if child is not None else ""


def _parse_xml(content):
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _s3_error(resp):
    """从 S3 错误响应里提取 [Code] Message。"""
    root = _parse_xml(resp.text or "")
    code = message = None
    if root is not None:
        for el in root.iter():
            if _local(el.tag) == "Code":
                code = el.text
            elif _local(el.tag) == "Message":
                message = el.text
    if not code and not message:
        return "HTTP %s %s" % (resp.status_code, resp.reason)
    return "[%s] %s" % (code or resp.status_code, message or "")


def _require(args, *names):
    missing = [n for n in names if not str(args.get(n, "") or "").strip()]
    if missing:
        raise ObjStoreError("缺少必填参数: %s" % ", ".join(missing), code="bad_request")
    return {n: str(args[n]).strip() for n in names}


def _join(bucket, key):
    if key is None:
        return "/" + bucket
    key = "" if key is None else str(key)
    return "/%s/%s" % (bucket, key)


# ============================ 工具实现 ============================
def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def action_upload(args):
    p = _require(args, "bucket", "key", "local_path")
    endpoint, region, ak, sk = _conn(args)
    timeout = _timeout(args)
    local_path = p["local_path"]
    if not os.path.isfile(local_path):
        return _error("本地文件不存在: %s" % local_path, code="not_found")

    size = os.path.getsize(local_path)
    content_type = mimetypes.guess_type(p["key"])[0] or "application/octet-stream"
    payload_hash = _file_sha256(local_path)
    with open(local_path, "rb") as f:
        resp = _request(endpoint, region, ak, sk, "PUT", _join(p["bucket"], p["key"]),
                        data_stream=f, payload_hash=payload_hash, content_type=content_type,
                        timeout=timeout)
    if resp.status_code not in (200, 201):
        return _error("上传失败: %s" % _s3_error(resp), code="upload_failed")
    return _ok({
        "bucket": p["bucket"], "key": p["key"], "local_path": local_path,
        "size": size,
        "message": "已上传: %s -> s3://%s/%s (%d 字节)" % (local_path, p["bucket"], p["key"], size),
    })


def action_download(args):
    p = _require(args, "bucket", "key", "local_path")
    endpoint, region, ak, sk = _conn(args)
    timeout = _timeout(args)
    local_path = p["local_path"]

    resp = _request(endpoint, region, ak, sk, "GET", _join(p["bucket"], p["key"]),
                    timeout=timeout, stream=True)
    if resp.status_code != 200:
        message = _s3_error(resp)
        resp.close()
        return _error("下载失败: %s" % message, code="download_failed")

    parent = os.path.dirname(os.path.abspath(local_path))
    os.makedirs(parent, exist_ok=True)
    written = 0
    try:
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
    finally:
        resp.close()
    return _ok({
        "bucket": p["bucket"], "key": p["key"], "local_path": local_path, "size": written,
        "message": "已下载: s3://%s/%s -> %s (%d 字节)" % (p["bucket"], p["key"], local_path, written),
    })


def action_delete(args):
    p = _require(args, "bucket", "key")
    endpoint, region, ak, sk = _conn(args)
    resp = _request(endpoint, region, ak, sk, "DELETE", _join(p["bucket"], p["key"]),
                    timeout=_timeout(args))
    if resp.status_code in (200, 204):
        return _ok({"bucket": p["bucket"], "key": p["key"],
                    "message": "已删除: s3://%s/%s" % (p["bucket"], p["key"])})
    return _error("删除失败: %s" % _s3_error(resp), code="delete_failed")


def _delete_keys(endpoint, region, ak, sk, bucket, keys, timeout):
    xml_body = "<Delete>" + "".join(
        "<Object><Key>%s</Key></Object>" % _xml_escape(k) for k in keys
    ) + "</Delete>"
    resp = _request(endpoint, region, ak, sk, "POST", _join(bucket, None),
                    query={"delete": ""}, data=xml_body.encode("utf-8"),
                    content_type="application/xml", timeout=timeout)
    return resp


def action_delete_batch(args):
    p = _require(args, "bucket")
    raw = args.get("keys")
    if not isinstance(raw, (list, tuple)) or not raw:
        return _error("缺少 keys 参数（非空字符串数组）", code="bad_request")
    keys = [str(k) for k in raw]
    endpoint, region, ak, sk = _conn(args)
    timeout = _timeout(args)

    deleted = 0
    for batch in [keys[i:i + BATCH_SIZE] for i in range(0, len(keys), BATCH_SIZE)]:
        resp = _delete_keys(endpoint, region, ak, sk, p["bucket"], batch, timeout)
        if resp.status_code not in (200, 204):
            return _error("批量删除失败: %s" % _s3_error(resp), code="delete_failed")
        deleted += len(batch)
    return _ok({"bucket": p["bucket"], "deleted": deleted,
                "message": "已批量删除 %d 个对象" % deleted})


def _list_page(endpoint, region, ak, sk, bucket, prefix, max_keys,
               continuation_token, delimiter, timeout):
    query = {"list-type": "2", "max-keys": str(max_keys)}
    if prefix:
        query["prefix"] = prefix
    if continuation_token:
        query["continuation-token"] = continuation_token
    if delimiter:
        query["delimiter"] = delimiter
    return _request(endpoint, region, ak, sk, "GET", _join(bucket, None),
                    query=query, timeout=timeout)


def _parse_list(xml_bytes):
    root = _parse_xml(xml_bytes)
    if root is None:
        raise ObjStoreError("对象列表返回无法解析", code="bad_response")
    objects = []
    for node in _findall(root, "Contents"):
        objects.append({
            "key": _text(node, "Key"),
            "size": int(_text(node, "Size") or 0),
            "last_modified": _text(node, "LastModified"),
            "etag": _text(node, "ETag").strip('"'),
            "storage_class": _text(node, "StorageClass"),
        })
    common_prefixes = [_text(node, "Prefix") for node in _findall(root, "CommonPrefixes")]
    truncated = _text(root, "IsTruncated").lower() == "true"
    next_token = _text(root, "NextContinuationToken") if truncated else ""
    return objects, common_prefixes, truncated, next_token


def action_list(args):
    p = _require(args, "bucket")
    endpoint, region, ak, sk = _conn(args)
    timeout = _timeout(args)
    prefix = str(args.get("prefix", "") or "")
    delimiter = str(args.get("delimiter", "") or "")
    try:
        max_keys = int(args.get("max_keys", MAX_KEYS_DEFAULT))
    except (TypeError, ValueError):
        return _error("arguments.max_keys 必须是整数", code="bad_request")

    resp = _list_page(endpoint, region, ak, sk, p["bucket"], prefix, max_keys,
                      str(args.get("continuation_token", "") or ""), delimiter, timeout)
    if resp.status_code != 200:
        return _error("列举失败: %s" % _s3_error(resp), code="list_failed")

    objects, common_prefixes, truncated, next_token = _parse_list(resp.content)
    result = {
        "bucket": p["bucket"], "prefix": prefix, "count": len(objects),
        "truncated": truncated, "objects": objects,
    }
    if common_prefixes:
        result["common_prefixes"] = common_prefixes
    if next_token:
        result["next_continuation_token"] = next_token
    return _ok(result)


def _list_all_keys(endpoint, region, ak, sk, bucket, prefix, timeout):
    keys = []
    token = ""
    while True:
        resp = _list_page(endpoint, region, ak, sk, bucket, prefix, BATCH_SIZE, token, "", timeout)
        if resp.status_code != 200:
            raise ObjStoreError("列举失败: %s" % _s3_error(resp), code="list_failed")
        objects, _prefixes, truncated, token = _parse_list(resp.content)
        keys.extend(o["key"] for o in objects if o["key"])
        if not truncated or not token:
            break
    return keys


def action_copy(args):
    p = _require(args, "bucket", "key", "source_key")
    endpoint, region, ak, sk = _conn(args)
    source_bucket = str(args.get("source_bucket") or p["bucket"]).strip()
    source = quote("/%s/%s" % (source_bucket, p["source_key"]), safe="/")
    resp = _request(endpoint, region, ak, sk, "PUT", _join(p["bucket"], p["key"]),
                    headers={"x-amz-copy-source": source}, data=b"",
                    content_type="application/octet-stream", timeout=_timeout(args))
    if resp.status_code != 200:
        return _error("复制失败: %s" % _s3_error(resp), code="copy_failed")
    return _ok({"message": "已复制: s3://%s/%s -> s3://%s/%s"
                % (source_bucket, p["source_key"], p["bucket"], p["key"])})


def action_head(args):
    p = _require(args, "bucket", "key")
    endpoint, region, ak, sk = _conn(args)
    resp = _request(endpoint, region, ak, sk, "HEAD", _join(p["bucket"], p["key"]),
                    timeout=_timeout(args))
    if resp.status_code != 200:
        return _error("获取元数据失败: %s" % _s3_error(resp), code="head_failed")
    return _ok({
        "bucket": p["bucket"], "key": p["key"],
        "size": int(resp.headers.get("content-length", 0) or 0),
        "content_type": resp.headers.get("content-type", ""),
        "etag": resp.headers.get("etag", "").strip('"'),
        "last_modified": resp.headers.get("last-modified", ""),
    })


def action_list_buckets(args):
    endpoint, region, ak, sk = _conn(args)
    resp = _request(endpoint, region, ak, sk, "GET", "/", timeout=_timeout(args))
    if resp.status_code != 200:
        return _error("列举 bucket 失败: %s" % _s3_error(resp), code="list_failed")

    root = _parse_xml(resp.content)
    buckets = []
    if root is not None:
        holder = _find(root, "Buckets") or root
        for node in _findall(holder, "Bucket"):
            buckets.append({"name": _text(node, "Name"),
                            "created": _text(node, "CreationDate")})
    return _ok({"count": len(buckets), "buckets": buckets})


def action_create_bucket(args):
    p = _require(args, "bucket")
    endpoint, region, ak, sk = _conn(args)
    resp = _request(endpoint, region, ak, sk, "PUT", _join(p["bucket"], None),
                    data=b"", content_type="application/octet-stream", timeout=_timeout(args))
    if resp.status_code not in (200, 201):
        return _error("创建 bucket 失败: %s" % _s3_error(resp), code="create_bucket_failed")
    return _ok({"bucket": p["bucket"], "message": "已创建 bucket: %s" % p["bucket"]})


def action_delete_bucket(args):
    p = _require(args, "bucket")
    endpoint, region, ak, sk = _conn(args)
    resp = _request(endpoint, region, ak, sk, "DELETE", _join(p["bucket"], None),
                    timeout=_timeout(args))
    if resp.status_code in (200, 204):
        return _ok({"bucket": p["bucket"], "message": "已删除 bucket: %s" % p["bucket"]})
    return _error("删除 bucket 失败: %s" % _s3_error(resp), code="delete_bucket_failed")


def action_presign(args):
    p = _require(args, "bucket", "key")
    endpoint, region, ak, sk = _conn(args)
    method = str(args.get("method") or "GET").strip().upper()
    if method not in ("GET", "PUT"):
        return _error("arguments.method 只能是 GET / PUT", code="bad_request")
    try:
        expires = int(args.get("expires", 3600))
    except (TypeError, ValueError):
        return _error("arguments.expires 必须是整数秒", code="bad_request")
    if expires <= 0:
        return _error("arguments.expires 必须为正整数秒", code="bad_request")

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = _host_of(endpoint)
    canon_path = _canonical_path(_join(p["bucket"], p["key"]))
    credential_scope = "%s/%s/s3/aws4_request" % (date_stamp, region)

    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": "%s/%s" % (ak, credential_scope),
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canon_query = _canonical_query(query)
    canonical_request = "\n".join(
        [method, canon_path, canon_query, "host:%s\n" % host, "host", _UNSIGNED_PAYLOAD]
    )
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])
    signature = hmac.new(
        _signing_key(sk, date_stamp, region), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    url = "%s%s?%s&X-Amz-Signature=%s" % (endpoint, canon_path, canon_query, signature)
    return _ok({"url": url, "method": method, "expires": expires,
                "bucket": p["bucket"], "key": p["key"]})


def action_create_folder(args):
    p = _require(args, "bucket", "folder")
    endpoint, region, ak, sk = _conn(args)
    folder_key = p["folder"].strip("/") + "/"
    resp = _request(endpoint, region, ak, sk, "PUT", _join(p["bucket"], folder_key),
                    data=b"", content_type="application/octet-stream", timeout=_timeout(args))
    if resp.status_code not in (200, 201):
        return _error("创建文件夹失败: %s" % _s3_error(resp), code="create_folder_failed")
    return _ok({"bucket": p["bucket"], "folder": folder_key,
                "message": "已创建文件夹: s3://%s/%s" % (p["bucket"], folder_key)})


def action_delete_folder(args):
    p = _require(args, "bucket", "folder")
    endpoint, region, ak, sk = _conn(args)
    timeout = _timeout(args)
    prefix = p["folder"].strip("/") + "/"
    keys = _list_all_keys(endpoint, region, ak, sk, p["bucket"], prefix, timeout)
    if not keys:
        return _ok({"bucket": p["bucket"], "folder": prefix, "deleted": 0,
                    "message": "文件夹为空，无需删除: s3://%s/%s" % (p["bucket"], prefix)})

    for batch in [keys[i:i + BATCH_SIZE] for i in range(0, len(keys), BATCH_SIZE)]:
        resp = _delete_keys(endpoint, region, ak, sk, p["bucket"], batch, timeout)
        if resp.status_code not in (200, 204):
            return _error("删除文件夹失败: %s" % _s3_error(resp), code="delete_failed")
    return _ok({"bucket": p["bucket"], "folder": prefix, "deleted": len(keys),
                "message": "已删除文件夹 s3://%s/%s 及其下 %d 个对象"
                           % (p["bucket"], prefix, len(keys))})


# ============================ 分发包装 ============================
def _guard(fn):
    def wrapper(args):
        try:
            return fn(args)
        except ObjStoreError as e:
            return _error(str(e), code=e.code)
        except requests.RequestException as e:
            return _error("网络请求失败: %s" % e, code="network_error")
        except Exception as e:  # noqa: BLE001 —— 兜底，避免异常穿透到 runner
            return _error("对象存储操作失败: %s" % e, code="internal_error")

    wrapper.__name__ = fn.__name__
    return wrapper


ACTIONS = {
    "upload": _guard(action_upload),
    "download": _guard(action_download),
    "delete": _guard(action_delete),
    "delete_batch": _guard(action_delete_batch),
    "list": _guard(action_list),
    "copy": _guard(action_copy),
    "head": _guard(action_head),
    "list_buckets": _guard(action_list_buckets),
    "create_bucket": _guard(action_create_bucket),
    "delete_bucket": _guard(action_delete_bucket),
    "presign": _guard(action_presign),
    "create_folder": _guard(action_create_folder),
    "delete_folder": _guard(action_delete_folder),
}


def dispatch(arguments):
    a = arguments or {}
    action = str(a.get("action") or "").strip()
    fn = ACTIONS.get(action)
    if fn is None:
        return _error("未知 action '%s'，可用: %s" % (action, ", ".join(ACTIONS)),
                      code="unknown_action")
    return fn(a)
