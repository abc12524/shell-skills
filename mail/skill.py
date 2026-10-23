# skill: mail
# description: 多邮箱工具（163/126/yeah/qq/gmail/outlook）：列表 / 读信 / 发信 / 未读
# status: enable
# usage:
#   arguments.action   = list | read | send | monitor | folders | providers | config（默认 list）
#   arguments.provider = 163 | 126 | yeah | qq | gmail | outlook（默认 163）
#   arguments.folder   = 操作文件夹（list/read/monitor 用，默认 INBOX；见 folders）
#   arguments.args     = read 传 [邮件ID]（HTML 正文自动转纯文本）；send 传 [收件人, 主题, 正文]
#   arguments.max_len  = read 正文截断长度（默认 8000）
#   账号密码：arguments.user/password > config.json accounts.<provider> > 环境变量 MAIL_USER/MAIL_PASS
#   示例: {"action":"list","provider":"qq"}
#         {"action":"folders"}
#         {"action":"read","args":["3"],"folder":"Sent"}
#         {"action":"send","args":["a@b.com","标题","正文"]}
# 配置：同目录 config.json（模板 config.example.json），accounts.<provider>={"user","pass"}；可选 MAIL_CA_BUNDLE
# 依赖：仅标准库
"""多邮箱工具 skill（列表 / 读信 / 发信 / 未读）。"""
import base64
import email
import email.policy
import imaplib
import json
import os
import re
import smtplib
import ssl
from html import unescape as _unescape

from email.message import EmailMessage

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

# 移动端默认证书包（不存在时回退系统默认）。
DEFAULT_CA_BUNDLE = "/data/user/0/com.androidagent/files/ca_bundle.crt"

# 正文默认截断长度（可用 arguments.max_len 覆盖；HTML 邮件会先转纯文本）。
DEFAULT_MAX_BODY = 8000

PROVIDERS = {
    "163": dict(name="网易163邮箱", imap_host="imap.163.com", imap_port=143, imap_ssl=False,
                smtp_host="smtp.163.com", smtp_port=465, smtp_ssl=True, need_id_command=True,
                note="需开启IMAP/SMTP，使用授权码"),
    "126": dict(name="网易126邮箱", imap_host="imap.126.com", imap_port=143, imap_ssl=False,
                smtp_host="smtp.126.com", smtp_port=465, smtp_ssl=True, need_id_command=True,
                note="需开启IMAP/SMTP，使用授权码"),
    "yeah": dict(name="网易Yeah邮箱", imap_host="imap.yeah.net", imap_port=143, imap_ssl=False,
                 smtp_host="smtp.yeah.net", smtp_port=465, smtp_ssl=True, need_id_command=True,
                 note="需开启IMAP/SMTP，使用授权码"),
    "qq": dict(name="QQ邮箱", imap_host="imap.qq.com", imap_port=993, imap_ssl=True,
               smtp_host="smtp.qq.com", smtp_port=465, smtp_ssl=True, need_id_command=False,
               note="设置→账户→POP3/IMAP/SMTP服务→生成授权码"),
    "gmail": dict(name="Gmail", imap_host="imap.gmail.com", imap_port=993, imap_ssl=True,
                  smtp_host="smtp.gmail.com", smtp_port=587, smtp_ssl=False, smtp_starttls=True,
                  need_id_command=False, note="需开启IMAP访问 + 应用专用密码"),
    "outlook": dict(name="Outlook/Hotmail", imap_host="outlook.office365.com", imap_port=993,
                    imap_ssl=True, smtp_host="smtp.office365.com", smtp_port=587, smtp_ssl=False,
                    smtp_starttls=True, need_id_command=False, note="使用邮箱密码或应用专用密码"),
}

ID_ARGS = '("name" "mailcli" "version" "1.0.0" "vendor" "mailcli" "support-email" "noreply@example.com")'


# ============= 配置 =============
_CONFIG = {}


def _load_config():
    """读取本技能文件夹内的 config.json。

    配置与脚本同目录：mail/config.json（模板见 config.example.json）。
    既支持按服务商分组的 accounts，也支持顶层环境变量键（注入 os.environ）。
    """
    global _CONFIG
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(cfg, dict):
        return
    _CONFIG = cfg
    for key, value in cfg.items():
        if isinstance(value, str) and value and not os.environ.get(key):
            os.environ[key] = value


def _account(provider):
    """取 config.json 中 accounts.<provider> 的 {user, pass}，无则回退顶层 MAIL_USER/MAIL_PASS。"""
    acc = (_CONFIG.get("accounts") or {}).get(provider) or {}
    user = acc.get("user") or acc.get("username") or os.environ.get("MAIL_USER")
    password = acc.get("pass") or acc.get("password") or os.environ.get("MAIL_PASS")
    return user, password


def _ssl_ctx():
    ca_bundle = os.environ.get("MAIL_CA_BUNDLE") or DEFAULT_CA_BUNDLE
    if ca_bundle and os.path.exists(ca_bundle):
        return ssl.create_default_context(cafile=ca_bundle)
    return ssl.create_default_context()


def _imap(cfg, user, password):
    if cfg["imap_ssl"]:
        conn = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=_ssl_ctx())
    else:
        conn = imaplib.IMAP4(cfg["imap_host"], cfg["imap_port"])
    conn.login(user, password)
    if cfg.get("need_id_command"):
        # 163/126/yeah 要求登录后（AUTH 状态）发送 ID 命令
        imaplib.Commands["ID"] = ("AUTH", "SELECTED")
        typ, dat = conn._simple_command("ID", ID_ARGS)
        conn._untagged_response(typ, dat, "ID")
    return conn


def _smtp(cfg, user, password):
    ctx = _ssl_ctx()
    if cfg["smtp_ssl"]:
        conn = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=ctx)
    else:
        conn = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"])
        if cfg.get("smtp_starttls"):
            conn.starttls(context=ctx)
    conn.login(user, password)
    return conn


def _msg(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


def _looks_like_html(text):
    return bool(re.search(r"(?is)<(html|body|div|table|p|style|span|a)\b", text or ""))


def _html_to_text(html):
    """把 HTML 邮件正文转为可读纯文本（去 style/script、标签，解实体）。"""
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", "", html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)<(p|div|h[1-6]|tr|table|ul|ol|blockquote)[^>]*>", "\n", text)
    text = re.sub(r"(?is)</(p|div|h[1-6]|tr|table|ul|ol|blockquote|li)>", "\n", text)
    text = re.sub(r"(?is)</t[dh]>", "\t", text)
    text = re.sub(r"(?is)<td[^>]*>", "", text)
    text = re.sub(r"(?is)</?li[^>]*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = _unescape(text)
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    out, blank = [], 0
    for line in text.split("\n"):
        line = line.rstrip()
        if line:
            out.append(line)
            blank = 0
        elif blank == 0:
            out.append("")
            blank = 1
    return "\n".join(out).strip()


def _decode_utf7(s):
    """IMAP modified UTF-7 → str。"""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c != "&":
            out.append(c)
            i += 1
            continue
        j = s.find("-", i)
        if j == i + 1:  # "&-" 表示 &
            out.append("&")
            i = j + 1
            continue
        if j == -1:
            j = len(s)
        b64 = s[i + 1:j].replace(",", "/")
        try:
            out.append(base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-be"))
        except Exception:
            out.append(s[i:j])
        i = j + 1
    return "".join(out)


def _encode_utf7(s):
    """str → IMAP modified UTF-7（仅含非 ASCII 时与原文不同）。"""
    out, buf = [], ""
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            if buf:
                out.append("&" + base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-")
                buf = ""
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    if buf:
        out.append("&" + base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-")
    return "".join(out)


def _select(conn, folder):
    """选择邮箱文件夹（默认 INBOX）；非 ASCII 名称按 modified UTF-7 编码。"""
    name = folder or "INBOX"
    wire = _encode_utf7(name) if any(ord(c) > 127 for c in name) else name
    conn.select(wire)
    return name


def _parse_mailboxes(data):
    boxes = []
    for raw in data or []:
        if not raw:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        m = re.match(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip()
        if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
            name = name[1:-1]
        boxes.append({"name": _decode_utf7(name), "delim": m.group("delim"),
                      "flags": m.group("flags").split()})
    return boxes


def _do_folders(cfg, user, password):
    conn = _imap(cfg, user, password)
    try:
        typ, data = conn.list()
        boxes = _parse_mailboxes(data)
        lines = ["📂 文件夹（%d）：" % len(boxes)]
        for b in boxes:
            flags = ("  [%s]" % ", ".join(b["flags"])) if b["flags"] else ""
            lines.append("  %s%s" % (b["name"], flags))
        return ok({"text": "\n".join(lines)})
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _do_providers():
    lines = ["📋 支持的邮箱服务商："]
    for k, p in PROVIDERS.items():
        lines.append("  %-8s %s" % (k, p["name"]))
        lines.append("          IMAP: %s %s:%s" % ("🔒" if p["imap_ssl"] else "🔓", p["imap_host"], p["imap_port"]))
        lines.append("          SMTP: %s %s:%s%s" % ("🔒" if p["smtp_ssl"] else "🔓", p["smtp_host"], p["smtp_port"],
                                                 " + STARTTLS" if p.get("smtp_starttls") else ""))
        lines.append("          备注: %s" % p["note"])
    return ok({"text": "\n".join(lines)})


def _do_list(cfg, user, password, folder="INBOX"):
    conn = _imap(cfg, user, password)
    try:
        _select(conn, folder)
        typ, data = conn.search(None, "ALL")
        ids = data[0].split()
        if not ids:
            return ok({"text": "📭 文件夹为空"})
        lines = ["📬 %s 共 %d 封（显示最近 20 封）" % (folder or "INBOX", len(ids))]
        for uid in ids[-20:]:
            typ, d = conn.fetch(uid, "(RFC822.HEADER)")
            m = _msg(d[0][1])
            item = {"id": uid.decode(), "subject": str(m.get("Subject", "(无主题)")),
                    "from": str(m.get("From", "(未知)")),
                    "date": str(m.get("Date", ""))[:25]}
            lines.append("  [%s] %s\n        From: %s  %s" % (item["id"], item["subject"][:60], item["from"][:40], item["date"]))
        return ok({"text": "\n".join(lines)})
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _do_read(cfg, user, password, uid, folder="INBOX", max_len=DEFAULT_MAX_BODY):
    conn = _imap(cfg, user, password)
    try:
        _select(conn, folder)
        typ, d = conn.fetch(str(uid), "(RFC822)")
        if typ != "OK" or not d or d[0] is None:
            return error("查无此邮件: %s" % uid, code="not_found")
        m = _msg(d[0][1])
        body, ctype, html = None, "text/plain", None
        if m.is_multipart():
            for part in m.walk():
                if "attachment" in str(part.get("Content-Disposition") or "").lower():
                    continue
                ct = part.get_content_type()
                if ct == "text/plain" and body is None:
                    body, ctype = part.get_content(), "text/plain"
                elif ct == "text/html" and html is None:
                    html = part.get_content()
            if body is None and html is not None:
                body, ctype = html, "text/html"
        else:
            body, ctype = m.get_content(), m.get_content_type()
        if ctype == "text/html" or _looks_like_html(body):
            body = _html_to_text(body)
        body = (body or "(空或无法解析)")[:max_len]
        head = "📌 主题: %s\n👤 发件人: %s\n📅 日期: %s" % (m.get("Subject", "(无主题)"),
                                                      m.get("From", "(未知)"), m.get("Date", ""))
        return ok({"text": head + "\n\n" + body})
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _do_send(cfg, user, password, to_addr, subject, body):
    conn = _smtp(cfg, user, password)
    try:
        m = EmailMessage()
        m["From"] = user
        m["To"] = to_addr
        m["Subject"] = subject
        m.set_content(body, charset="utf-8")
        conn.sendmail(user, [to_addr], m.as_bytes())
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    return ok({"text": "✅ 已发送 → %s" % to_addr})


def _do_monitor(cfg, user, password, folder="INBOX"):
    conn = _imap(cfg, user, password)
    try:
        _select(conn, folder)
        typ, data = conn.search(None, "UNSEEN")
        ids = data[0].split()
        if not ids:
            return ok({"text": "📭 没有新邮件"})
        lines = ["📬 有 %d 封新邮件" % len(ids)]
        for uid in ids[-5:]:
            typ, d = conn.fetch(uid, "(RFC822.HEADER)")
            m = _msg(d[0][1])
            item = {"id": uid.decode(), "subject": str(m.get("Subject", "?")), "from": str(m.get("From", "?"))}
            lines.append("  [%s] %s  —  %s" % (item["id"], item["subject"][:50], item["from"][:30]))
        return ok({"text": "\n".join(lines)})
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def run(arguments):
    """skill 入口：arguments(dict) → ok()/error() 信封字符串。"""
    a = arguments or {}
    _load_config()

    action = str(a.get("action") or "list").strip().lower()
    if action == "providers":
        return _do_providers()

    key = str(a.get("provider") or os.environ.get("MAIL_PROVIDER") or "163").lower()
    if key not in PROVIDERS:
        return error("不支持的邮箱服务商 %r，可用: %s" % (key, ", ".join(PROVIDERS)), code="invalid_argument")
    cfg = PROVIDERS[key]

    user, password = _account(key)
    user = a.get("user") or user
    password = a.get("password") or password
    if not user or not password:
        return error("缺少账号/密码：请在 config.json 的 accounts.%s 配置，或在 arguments 传 "
                     "user/password，或设置环境变量 MAIL_USER/MAIL_PASS" % key,
                     code="invalid_argument")

    argv = a.get("args") or []
    if isinstance(argv, str):
        argv = [argv]
    folder = a.get("folder") or "INBOX"
    try:
        max_len = int(a.get("max_len") or DEFAULT_MAX_BODY)
    except (TypeError, ValueError):
        max_len = DEFAULT_MAX_BODY

    if action == "config":
        return ok({"provider": key, "name": cfg["name"], "user": user,
                   "imap": "%s:%s" % (cfg["imap_host"], cfg["imap_port"]),
                   "smtp": "%s:%s" % (cfg["smtp_host"], cfg["smtp_port"])})
    try:
        if action == "folders":
            return _do_folders(cfg, user, password)
        if action == "list":
            return _do_list(cfg, user, password, folder)
        if action == "monitor":
            return _do_monitor(cfg, user, password, folder)
        if action == "read":
            if len(argv) < 1:
                return error('用法: {"action": "read", "args": [邮件ID]}', code="invalid_argument")
            return _do_read(cfg, user, password, argv[0], folder, max_len)
        if action == "send":
            if len(argv) < 3:
                return error('用法: {"action": "send", "args": [收件人, 主题, 正文]}', code="invalid_argument")
            return _do_send(cfg, user, password, argv[0], argv[1], argv[2])
        return error("未知命令 %r，可用: list / read / send / monitor / folders / providers / config" % action,
                     code="invalid_argument")
    except Exception as e:
        return error("%s 执行失败: %s" % (action, e), code="io_error")


if __name__ == "__main__":
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
