# skill: mail
# description: 多邮箱通用工具（163/126/yeah/qq/gmail/outlook）：列收件箱、读邮件、发邮件、查未读
# status: enable
# usage:
#   arguments.cmd      = list | read | send | monitor | providers | config（默认 list）
#   arguments.provider = 163 | 126 | yeah | qq | gmail | outlook（默认 163）
#   arguments.user     = 邮箱账号（缺省取 config.json accounts.<provider>.user，再回退 MAIL_USER）
#   arguments.password = 密码/授权码（缺省取 config.json accounts.<provider>.pass，再回退 MAIL_PASS）
#   arguments.args     = 参数列表：read [ID]；send [收件人, 主题, 正文]
#   示例: {"cmd": "list"}
#   示例: {"cmd": "read", "args": ["3"]}
#   示例: {"cmd": "send", "args": ["a@b.com", "标题", "正文"]}
#   示例: {"cmd": "list", "provider": "qq"}
#   示例: {"cmd": "providers"}
#
# 配置（同目录 config.json，模板见 config.example.json）：
#   accounts.<provider> = {"user": "...", "pass": "..."}  # 按服务商分别配置，可同时配多个
#   环境变量 MAIL_PROVIDER / MAIL_USER / MAIL_PASS（单账号）、MAIL_CA_BUNDLE。
#   优先级：arguments.{user,password} > config.json accounts.<provider> > 环境变量。
#   - shell-tool：由项目根 .env 提供（导入 core.config 时注入 os.environ）。
#   - android-agent / kelivo-agent：由本技能同目录 config.json 提供。
# 依赖：仅标准库（imaplib / smtplib / email）。
"""多邮箱通用工具 skill（列收件箱 / 读邮件 / 发邮件 / 查未读）。"""
import email
import email.policy
import imaplib
import json
import os
import smtplib
import ssl

from email.message import EmailMessage

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

# 移动端默认证书包（不存在时回退系统默认）。
DEFAULT_CA_BUNDLE = "/data/user/0/com.androidagent/files/ca_bundle.crt"

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


def _do_providers():
    lines = ["📋 支持的邮箱服务商："]
    for k, p in PROVIDERS.items():
        lines.append("  %-8s %s" % (k, p["name"]))
        lines.append("          IMAP: %s %s:%s" % ("🔒" if p["imap_ssl"] else "🔓", p["imap_host"], p["imap_port"]))
        lines.append("          SMTP: %s %s:%s%s" % ("🔒" if p["smtp_ssl"] else "🔓", p["smtp_host"], p["smtp_port"],
                                                 " + STARTTLS" if p.get("smtp_starttls") else ""))
        lines.append("          备注: %s" % p["note"])
    return ok({"providers": list(PROVIDERS), "text": "\n".join(lines)})


def _do_list(cfg, user, password):
    conn = _imap(cfg, user, password)
    try:
        conn.select("INBOX")
        typ, data = conn.search(None, "ALL")
        ids = data[0].split()
        if not ids:
            return ok({"total": 0, "mails": [], "text": "📭 收件箱为空"})
        mails, lines = [], ["📬 收件箱共 %d 封（显示最近 20 封）" % len(ids)]
        for uid in ids[-20:]:
            typ, d = conn.fetch(uid, "(RFC822.HEADER)")
            m = _msg(d[0][1])
            item = {"id": uid.decode(), "subject": str(m.get("Subject", "(无主题)")),
                    "from": str(m.get("From", "(未知)")),
                    "date": str(m.get("Date", ""))[:25]}
            mails.append(item)
            lines.append("  [%s] %s\n        From: %s  %s" % (item["id"], item["subject"][:60], item["from"][:40], item["date"]))
        return ok({"total": len(ids), "mails": mails, "text": "\n".join(lines)})
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _do_read(cfg, user, password, uid):
    conn = _imap(cfg, user, password)
    try:
        conn.select("INBOX")
        typ, d = conn.fetch(str(uid), "(RFC822)")
        if typ != "OK" or not d or d[0] is None:
            return error("查无此邮件: %s" % uid, code="not_found")
        m = _msg(d[0][1])
        body = None
        if m.is_multipart():
            for part in m.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    body = part.get_content(); break
                if ct == "text/html" and not body:
                    body = part.get_content()
        else:
            body = m.get_content()
        body = (body or "(空或无法解析)")[:3000]
        head = "📌 主题: %s\n👤 发件人: %s\n📅 日期: %s" % (m.get("Subject", "(无主题)"),
                                                      m.get("From", "(未知)"), m.get("Date", ""))
        return ok({"id": str(uid), "subject": str(m.get("Subject", "")), "body": body,
                   "text": head + "\n\n" + body})
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
    return ok({"to": to_addr, "subject": subject, "text": "✅ 已发送 → %s" % to_addr})


def _do_monitor(cfg, user, password):
    conn = _imap(cfg, user, password)
    try:
        conn.select("INBOX")
        typ, data = conn.search(None, "UNSEEN")
        ids = data[0].split()
        if not ids:
            return ok({"unread": 0, "mails": [], "text": "📭 没有新邮件"})
        mails, lines = [], ["📬 有 %d 封新邮件" % len(ids)]
        for uid in ids[-5:]:
            typ, d = conn.fetch(uid, "(RFC822.HEADER)")
            m = _msg(d[0][1])
            item = {"id": uid.decode(), "subject": str(m.get("Subject", "?")), "from": str(m.get("From", "?"))}
            mails.append(item)
            lines.append("  [%s] %s  —  %s" % (item["id"], item["subject"][:50], item["from"][:30]))
        return ok({"unread": len(ids), "mails": mails, "text": "\n".join(lines)})
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def run(arguments):
    """skill 入口：arguments(dict) → ok()/error() 信封字符串。"""
    a = arguments or {}
    _load_config()

    cmd = str(a.get("cmd") or "list").strip().lower()
    if cmd == "providers":
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

    if cmd == "config":
        return ok({"provider": key, "name": cfg["name"], "user": user,
                   "imap": "%s:%s" % (cfg["imap_host"], cfg["imap_port"]),
                   "smtp": "%s:%s" % (cfg["smtp_host"], cfg["smtp_port"])})
    try:
        if cmd == "list":
            return _do_list(cfg, user, password)
        if cmd == "monitor":
            return _do_monitor(cfg, user, password)
        if cmd == "read":
            if len(argv) < 1:
                return error('用法: {"cmd": "read", "args": [邮件ID]}', code="invalid_argument")
            return _do_read(cfg, user, password, argv[0])
        if cmd == "send":
            if len(argv) < 3:
                return error('用法: {"cmd": "send", "args": [收件人, 主题, 正文]}', code="invalid_argument")
            return _do_send(cfg, user, password, argv[0], argv[1], argv[2])
        return error("未知命令 %r，可用: list / read / send / monitor / providers / config" % cmd,
                     code="invalid_argument")
    except Exception as e:
        return error("%s 执行失败: %s" % (cmd, e), code="io_error")


if __name__ == "__main__":
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
