"""Deliver verification codes without exposing codes or SMTP credentials in logs."""
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import settings


def send_registration_code(email: str, code: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise RuntimeError("SMTP is not configured")
    message = EmailMessage()
    message["Subject"] = "Agent Harness 注册验证码"
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(f"你的注册验证码是：{code}\n\n验证码 10 分钟内有效，请勿分享给他人。\n如果不是你本人操作，请忽略这封邮件。")
    transport = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
    kwargs = {"timeout": 15}
    if settings.smtp_ssl:
        kwargs["context"] = ssl.create_default_context()
    with transport(settings.smtp_host, settings.smtp_port, **kwargs) as smtp:
        if not settings.smtp_ssl and settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
