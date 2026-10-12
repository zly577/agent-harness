"""Sender presets; recipient email domains are never restricted to this list."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SmtpProfile:
    host: str
    port: int
    ssl: bool
    auth: str = "password"


SMTP_PROFILES = {
    "qq": SmtpProfile("smtp.qq.com", 465, True),
    "163": SmtpProfile("smtp.163.com", 465, True),
    "126": SmtpProfile("smtp.126.com", 465, True),
    "tencent-enterprise": SmtpProfile("smtp.exmail.qq.com", 465, True),
    "netease-enterprise": SmtpProfile("smtphz.qiye.163.com", 465, True),
    "outlook": SmtpProfile("smtp-mail.outlook.com", 587, False, "oauth2"),
    "microsoft365": SmtpProfile("smtp.office365.com", 587, False, "oauth2"),
}


def resolve_smtp(config):
    provider = config.smtp_provider.strip().lower()
    if provider == "custom":
        profile = SmtpProfile(config.smtp_host, config.smtp_port, config.smtp_ssl)
        starttls = config.smtp_starttls and not profile.ssl
    else:
        if provider not in SMTP_PROFILES:
            raise ValueError("SMTP_PROVIDER 不支持，请选择预设或 custom")
        profile = SMTP_PROFILES[provider]
        starttls = not profile.ssl
    auth = profile.auth if config.smtp_auth_mode == "auto" else config.smtp_auth_mode
    if auth not in {"password", "oauth2"}:
        raise ValueError("SMTP_AUTH_MODE 只能为 auto、password 或 oauth2")
    if not profile.host or not config.smtp_from:
        raise ValueError("请配置 SMTP_HOST 和 SMTP_FROM，或选择 SMTP_PROVIDER 预设")
    if config.smtp_username and not (profile.ssl or starttls):
        raise ValueError("使用账号认证时必须启用 SSL 或 STARTTLS")
    return profile, starttls, auth
