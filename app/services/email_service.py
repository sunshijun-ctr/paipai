"""Send transactional emails via SMTP.

If SMTP_HOST is empty (typical in local dev), falls back to printing the
message to stdout so flows can be tested end-to-end without real email."""
from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)


async def send_email(*, to: str, subject: str, html: str, text: Optional[str] = None) -> None:
    """Send an HTML+text email. In dev (no SMTP_HOST), log to console."""
    if not settings.smtp_host:
        _log_dev(to=to, subject=subject, html=html, text=text)
        return

    msg = EmailMessage()
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_address}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text or _strip_tags(html))
    msg.add_alternative(html, subtype="html")

    import aiosmtplib
    await aiosmtplib.send(
        msg,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_password,
        use_tls=settings.smtp_use_tls,
    )
    logger.info("Sent email to %s — %s", to, subject)


# ── Templates ─────────────────────────────────────────────────────────────
# Email clients (Gmail / Outlook / QQ Mail / 163 …) strip <style> blocks and
# disallow most modern CSS. Everything below is INLINE styles only — that's
# why we don't reuse the landing.html palette via CSS variables.

# paipai purple palette (matches the landing & SVG logo gradient)
_P_DEEP   = "#3B348A"      # deep indigo
_P_MAIN   = "#534AB7"      # primary
_P_VIVID  = "#9B6FD4"      # vibrant
_P_SOFT   = "#F0EBFB"      # lavender wash (page bg)
_P_LINE   = "#E5DEFB"      # soft border
_P_INK    = "#1A1530"
_P_INK_3  = "#6B5F8A"


def _paipai_email_shell(title: str, body_html: str, footer_html: str = "") -> str:
    """Common email layout: lavender background + white card + paipai header."""
    return f"""\
<div style="margin:0;padding:32px 16px;background:{_P_SOFT};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:{_P_INK};line-height:1.65;">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid {_P_LINE};border-radius:18px;overflow:hidden;box-shadow:0 24px 60px rgba(83,74,183,0.12);">
    <!-- Header with paipai wordmark on a soft gradient strip -->
    <div style="padding:22px 28px 18px;background:linear-gradient(110deg,{_P_DEEP} 0%,{_P_MAIN} 50%,{_P_VIVID} 100%);">
      <table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
        <tr>
          <td style="padding-right:10px;vertical-align:middle;">
            <!-- Static "pp" logo: back letter slightly offset + front letter overlap, color-matched to the SVG gradient -->
            <div style="position:relative;width:48px;height:46px;">
              <span style="position:absolute;left:0;top:6px;font-family:system-ui,sans-serif;font-size:42px;font-weight:600;color:{_P_VIVID};opacity:0.72;line-height:1;">p</span>
              <span style="position:absolute;left:16px;top:0px;font-family:system-ui,sans-serif;font-size:42px;font-weight:600;color:#ffffff;opacity:0.98;line-height:1;text-shadow:0 1px 0 rgba(0,0,0,0.10);">p</span>
            </div>
          </td>
          <td style="vertical-align:middle;">
            <div style="font-family:system-ui,sans-serif;font-size:24px;font-weight:600;color:#ffffff;letter-spacing:0.04em;">paipai</div>
            <div style="font-size:12px;color:rgba(255,255,255,0.78);letter-spacing:0.06em;margin-top:2px;">你的 AI 科研助手</div>
          </td>
        </tr>
      </table>
    </div>

    <!-- Body card -->
    <div style="padding:30px 32px 28px;">
      <h2 style="font-size:20px;font-weight:700;color:{_P_INK};margin:0 0 14px 0;letter-spacing:-0.01em;">{title}</h2>
      {body_html}
    </div>

    <!-- Footer -->
    <div style="padding:16px 32px 20px;border-top:1px solid {_P_LINE};color:{_P_INK_3};font-size:12px;background:#fafbfd;">
      {footer_html or 'paipai · 一个为研究者打造的智能助手'}
    </div>
  </div>
</div>"""


def _branded_button(text: str, link: str) -> str:
    return (
        f'<a href="{link}" style="display:inline-block;padding:12px 26px;'
        f'background:linear-gradient(135deg,{_P_MAIN} 0%,{_P_DEEP} 60%,{_P_VIVID} 100%);'
        f'color:#ffffff;text-decoration:none;border-radius:10px;'
        f'font-weight:600;font-size:14px;letter-spacing:0.02em;'
        f'box-shadow:0 8px 18px rgba(83,74,183,0.30);">{text}</a>'
    )


def render_activation_email(*, display_name: str, link: str) -> tuple[str, str, str]:
    subject = "请激活你的 paipai 账号"
    body = f"""\
<p style="margin:0 0 12px 0;">你好 <b>{_escape(display_name)}</b>，</p>
<p style="margin:0 0 22px 0;color:{_P_INK_3};">欢迎加入 paipai。请点击下面的按钮激活账号，链接 <b>1 小时内有效</b>。</p>
<p style="margin:0 0 22px 0;">{_branded_button("激活账号", link)}</p>
<p style="margin:0 0 4px 0;color:{_P_INK_3};font-size:12px;">如果按钮无法点击，请复制以下链接到浏览器打开：</p>
<p style="margin:0 0 18px 0;word-break:break-all;"><a href="{link}" style="color:{_P_MAIN};font-size:12px;">{link}</a></p>
<p style="margin:18px 0 0 0;color:{_P_INK_3};font-size:12px;">如果不是你本人操作，请忽略此邮件。</p>"""
    html = _paipai_email_shell("欢迎加入 paipai", body)
    text = f"欢迎加入 paipai，{display_name}！\n激活你的账号：{link}\n（链接 1 小时内有效）"
    return subject, html, text


def render_reset_email(*, display_name: str, link: str) -> tuple[str, str, str]:
    subject = "paipai · 重置你的密码"
    body = f"""\
<p style="margin:0 0 12px 0;">你好 <b>{_escape(display_name)}</b>，</p>
<p style="margin:0 0 22px 0;color:{_P_INK_3};">你（或他人）请求重置 paipai 账号的密码。点击下方按钮设置新密码，链接 <b>30 分钟内有效</b>。</p>
<p style="margin:0 0 22px 0;">{_branded_button("设置新密码", link)}</p>
<p style="margin:0 0 4px 0;color:{_P_INK_3};font-size:12px;">如果按钮无法点击，请复制以下链接到浏览器打开：</p>
<p style="margin:0 0 18px 0;word-break:break-all;"><a href="{link}" style="color:{_P_MAIN};font-size:12px;">{link}</a></p>
<p style="margin:18px 0 0 0;color:{_P_INK_3};font-size:12px;">如果不是你本人操作，请忽略此邮件 — 你的密码不会被修改。</p>"""
    html = _paipai_email_shell("重置密码", body)
    text = f"paipai 密码重置链接：{link}\n（链接 30 分钟内有效）"
    return subject, html, text


# ── Helpers ───────────────────────────────────────────────────────────────

def _log_dev(*, to: str, subject: str, html: str, text: Optional[str]) -> None:
    body = text or _strip_tags(html)
    banner = "=" * 60
    logger.warning(
        "\n%s\n[DEV email — SMTP not configured]\nTo: %s\nSubject: %s\n\n%s\n%s",
        banner, to, subject, body, banner,
    )


def _strip_tags(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html).strip()


def _escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
