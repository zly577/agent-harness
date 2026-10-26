"""检查完整 Docker 部署，包括真实沙箱执行；无需模型 API Key。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--personal", action="store_true", help="检查 Windows 个人部署的权限策略")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    config = {}
    env_file = Path(os.environ.get("SMOKE_ENV_FILE", root / ".env"))
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
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
    status, body = request("/api/v1/access/config", token=token)
    assert status == 200, "权限配置检查失败"
    access = json.loads(body)
    assert access["mode"] == "sandbox", "默认权限不是 sandbox"
    assert ("full" in access["valid_modes"]) == args.personal, "完全访问策略与部署方式不符"
    if config.get("ALLOW_REGISTRATION", "false").lower() == "false":
        status, _ = request("/api/v1/auth/register", {
            "email": "docker-smoke@example.com", "password": "smoke-check-password",
            "verification_code": "123456",
        })
        assert status == 403, f"注册关闭策略检查失败：HTTP {status}"
        status, _ = request("/api/v1/auth/register/code", {
            "email": "docker-smoke@example.com",
        })
        assert status == 403, f"注册关闭时仍可请求验证码：HTTP {status}"
    # 服务健康并不代表执行容器可用：实际经过后端 SDK、服务代理和 execd 执行 Python。
    sandbox_check = '''
import uuid
from app.core.config import settings
from app.agent.opensandbox_backend import OpenSandboxBackend, registry
assert settings.sandbox_enabled, "OpenSandbox is disabled"
assert settings.sandbox_domain == "opensandbox:8080", "Internal sandbox address is not configured"
key = "deployment-smoke-" + uuid.uuid4().hex
try:
    result = OpenSandboxBackend(key).execute('python -c "print(6 * 7)"', timeout=30)
    assert result.exit_code == 0 and "42" in result.output, "Sandbox Python execution failed"
    print("OpenSandbox Python execution: PASS")
finally:
    registry.destroy(key)
'''
    subprocess.run(
        ["docker", "compose", "exec", "-T", "backend", "python", "-"],
        input=sandbox_check, text=True, cwd=root, check=True, timeout=420,
    )
    print("Docker 冒烟检查通过：页面、健康、登录、权限、注册策略与 OpenSandbox Python 执行。")


if __name__ == "__main__":
    main()
