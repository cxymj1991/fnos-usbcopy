"""手机推送渠道：PushPlus（微信）/ SMTP 邮件。

纯 Python 标准库实现（urllib / smtplib），无第三方依赖。
同步开始/完成/失败时由 syncengine._notify 调用，按 settings 中配置的渠道逐一推送。
"""

import json
import smtplib
import ssl
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

CHANNELS = ("pushplus", "smtp")


def enabled_channels(settings):
    """解析设置中的推送渠道列表。"""
    raw = settings.get("push_channels") or settings.get("push_channel") or ""
    if isinstance(raw, (list, tuple)):
        cands = [str(x).strip().lower() for x in raw]
    else:
        cands = [x.strip().lower()
                 for x in str(raw).replace("；", ";").replace("，", ",")
                 .replace(";", ",").split(",")]
    out = []
    for c in cands:
        if c in CHANNELS and c not in out:
            out.append(c)
    return out


def _htmlize(text):
    """PushPlus 使用 html 模板推送，纯文本换行不会渲染成换行。

    把换行转成 <br>，并转义 HTML 特殊字符，保证「出错文件清单」这类
    多行内容在微信里能正常分行显示（SMTP 走纯文本，不需要此处理）。
    """
    s = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _send_pushplus(settings, title, content):
    token = (settings.get("pushplus_token") or "").strip()
    if not token:
        return "未配置 Token"
    url = "https://www.pushplus.plus/send"
    payload = json.dumps({
        "token": token,
        "title": title,
        "content": _htmlize(content),
        "template": "html",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        j = json.loads(body)
        if j.get("code") == 200:
            return "ok"
        return "失败: %s" % (j.get("msg") or body[:200])
    except Exception:
        return "未知响应: " + body[:200]


def _send_smtp(settings, title, content):
    host = (settings.get("smtp_host") or "").strip()
    to_raw = (settings.get("smtp_to") or "").strip()
    if not host or not to_raw:
        return "未配置 SMTP 服务器/收件人"
    try:
        port = int(settings.get("smtp_port") or 465)
    except Exception:
        port = 465
    user = (settings.get("smtp_user") or "").strip()
    pwd = settings.get("smtp_pass") or ""
    frm = (settings.get("smtp_from") or user or "").strip()
    if not frm:
        return "未配置发件人/账号"
    to_list = [x.strip() for x in to_raw.replace("，", ",").split(",") if x.strip()]
    if not to_list:
        return "未配置收件人"

    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr(("USB Copy 同步", frm))
    msg["To"] = ", ".join(to_list)
    msg["Date"] = formatdate(localtime=True)

    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        smtp = smtplib.SMTP(host, port, timeout=20)
        try:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        except smtplib.SMTPException:
            pass
    try:
        if user:
            smtp.login(user, pwd)
        smtp.sendmail(frm, to_list, msg.as_string())
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return "ok"


_SENDERS = {
    "pushplus": _send_pushplus,
    "smtp": _send_smtp,
}


def send_push(settings, title, content):
    """按配置逐渠道推送。返回 [(channel, result)]；result=="ok" 表示成功。"""
    results = []
    if not settings.get("push_enabled", False):
        return results
    for c in enabled_channels(settings):
        fn = _SENDERS.get(c)
        if not fn:
            continue
        try:
            r = fn(settings, title, content)
        except Exception as e:
            r = "异常: %s" % e
        results.append((c, r))
    return results
