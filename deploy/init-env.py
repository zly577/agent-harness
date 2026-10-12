"""生成 Docker 配置，仅使用标准库，不覆盖已有 .env。"""
import base64
import os
from pathlib import Path
import secrets


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    target = root / ".env"
    values = {
        "MYSQL_PASSWORD": secrets.token_hex(24),
        "MYSQL_ROOT_PASSWORD": secrets.token_hex(24),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "FERNET_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "BOOTSTRAP_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "SANDBOX_API_KEY": secrets.token_urlsafe(32),
    }
    lines = []
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in values:
            line = f"{key}={values[key]}"
        lines.append(line)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("根目录 .env 已存在，已保留原配置。请直接编辑该文件。")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
    print("已生成根目录 .env。管理员密码请在文件中查看；请备份此文件。")


if __name__ == "__main__":
    main()
