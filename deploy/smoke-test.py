"""检查 Docker 基础部署，无需第三方依赖或模型 API Key。"""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    config = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            config[key] = value
    base = os.environ.get("SMOKE_BASE_URL", f"http://127.0.0.1:{config.get('WEB_PORT', '8088')}")

    def request(path, data=None, token=None, method=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    status, page = request("/")
    assert status == 200 and b'id="app"' in page, "前端页面检查失败"
    status, health = request("/health")
    assert status == 200 and json.loads(health)["status"] == "ok", "健康检查失败"
    status, body = request("/api/v1/auth/login", {
        "email": config["BOOTSTRAP_ADMIN_EMAIL"],
        "password": config["BOOTSTRAP_ADMIN_PASSWORD"],
    })
    assert status == 200, f"初始管理员登录失败：HTTP {status}"
    token = json.loads(body)["access_token"]
    status, body = request("/api/v1/auth/me", token=token)
    assert status == 200 and json.loads(body)["role"] == "admin", "管理员身份检查失败"
    status, body = request("/api/v1/access/config", {"mode": "full"}, token, "PUT")
    assert status == 400 and "禁用完全访问" in json.loads(body)["detail"], "full 模式没有被禁用"
    if config.get("ALLOW_REGISTRATION", "false").lower() == "false":
        status, _ = request("/api/v1/auth/register", {
            "email": "docker-smoke@example.com", "password": "smoke-check-password",
        })
        assert status == 403, "注册关闭策略检查失败"
    print("Docker 冒烟检查通过：页面、健康、登录、权限与注册策略。")


if __name__ == "__main__":
    main()
