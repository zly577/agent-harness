"""Run from backend: python -m scripts.check_smtp [--to your@email.com]."""
import argparse
import smtplib

from app.core.registration_mail import send_mail, smtp_connection


def main():
    parser = argparse.ArgumentParser(description="检查 SMTP 连接和登录；指定 --to 后发送一封测试邮件")
    parser.add_argument("--to", help="测试收件邮箱（不填则只检查连接和认证）")
    args = parser.parse_args()
    try:
        if args.to:
            send_mail(args.to, "Agent Harness 邮件服务测试", "邮件服务配置成功。这是一封测试邮件，不包含注册验证码。")
            print("测试邮件已交给邮件服务器，请查看收件箱或垃圾邮件。")
        else:
            with smtp_connection() as smtp:
                code, _ = smtp.noop()
                if code != 250:
                    raise RuntimeError("SMTP 服务未正常响应")
            print("SMTP 连接和认证检查通过。可加 --to 收件邮箱 验证投递。")
    except smtplib.SMTPAuthenticationError:
        raise SystemExit("认证失败：请检查邮箱授权码或 OAuth 权限，并确认已开启 SMTP 服务。") from None
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("邮件服务检查失败：请检查网络、端口、证书及发信权限。凭据未输出。") from None


if __name__ == "__main__":
    main()
