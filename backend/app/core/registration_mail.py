"""Deliver verification codes without exposing codes or SMTP credentials in logs."""
import smtplib
import ssl
import re
from contextlib import contextmanager
from email.message import EmailMessage

import httpx

from app.core.config import settings
from app.core.smtp_profiles import resolve_smtp


def microsoft_access_token() -> str:
    """Use delegated refresh tokens or an Exchange Online application identity."""
    if not settings.smtp_oauth_client_id:
        raise ValueError("微软邮箱需要配置 SMTP_OAUTH_CLIENT_ID 和 OAuth 授权")
    tenant = settings.smtp_oauth_tenant
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", tenant):
        raise ValueError("SMTP_OAUTH_TENANT 格式不正确")
    data = {"client_id": settings.smtp_oauth_client_id}
    if settings.smtp_oauth_client_secret:
        data["client_secret"] = settings.smtp_oauth_client_secret
    if settings.smtp_oauth_refresh_token:
        data.update(grant_type="refresh_token", refresh_token=settings.smtp_oauth_refresh_token,
                    scope="https://outlook.office.com/SMTP.Send offline_access")
    elif settings.smtp_oauth_client_secret and tenant not in {"common", "consumers", "organizations"}:
        data.update(grant_type="client_credentials", scope="https://outlook.office365.com/.default")
    else:
        raise ValueError("请配置 OAuth refresh token，或企业租户 ID 与应用密钥")
    # Never expose the token endpoint response body (it may contain credentials).
    try:
        response = httpx.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=data, timeout=15)
        response.raise_for_status()
        token = response.json()["access_token"]
        if not isinstance(token, str) or not token:
            raise ValueError()
        return token
    except Exception:
        raise RuntimeError("微软 OAuth 授权失败，请检查应用权限、租户和授权信息") from None


@contextmanager
def smtp_connection():
    profile, starttls, auth = resolve_smtp(settings)
    if auth == "oauth2" and not settings.smtp_username:
        raise ValueError("OAuth 发信需要设置 SMTP_USERNAME 为发件邮箱")
    transport = smtplib.SMTP_SSL if profile.ssl else smtplib.SMTP
    kwargs = {"timeout": 15}
    if profile.ssl:
        kwargs["context"] = ssl.create_default_context()
    with transport(profile.host, profile.port, **kwargs) as smtp:
        if starttls:
            smtp.starttls(context=ssl.create_default_context())
        if auth == "oauth2":
            token = microsoft_access_token()
            auth_string = f"user={settings.smtp_username}\x01auth=Bearer {token}\x01\x01"
            smtp.ehlo()
            smtp.auth("XOAUTH2", lambda challenge=None: auth_string if challenge is None else "")
        elif settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        yield smtp


def send_mail(email: str, subject: str, content: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(content)
    with smtp_connection() as smtp:
        smtp.send_message(message)


def send_registration_code(email: str, code: str) -> None:
    send_mail(email, "Agent Harness 注册验证码",
              f"你的注册验证码是：{code}\n\n验证码 10 分钟内有效，请勿分享给他人。\n如果不是你本人操作，请忽略这封邮件。")
