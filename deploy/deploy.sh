#!/usr/bin/env bash
# Agent Harness 服务器一键部署（Ubuntu 24.04 / Debian 12，含 MySQL 8.0 + Nginx + systemd）
# 用法：仓库已上传到 /opt/agent-harness 后执行：
#   sudo bash /opt/agent-harness/deploy/deploy.sh
# 若脚本带 Windows 回车符，先执行：sed -i 's/\r$//' /opt/agent-harness/deploy/deploy.sh

# Windows CRLF 自防护
sed -i 's/\r$//' "$0" 2>/dev/null || true

set -euo pipefail

APP_DIR=/opt/agent-harness
APP_USER="${SUDO_USER:-$(whoami)}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-CHANGE_ME_STRONG_PASSWORD}"

echo "==> [1/6] 安装系统依赖"
apt-get update
apt-get install -y python3.12-venv python3.12-dev mysql-server nginx apache2-utils

echo "==> [2/6] MySQL 建库建用户"
if mysql -u root -e "SELECT 1 FROM mysql.user WHERE user='harness' AND host='localhost'" | grep -q 1; then
  echo "    用户 harness 已存在，跳过"
else
  mysql -u root <<SQL
CREATE DATABASE IF NOT EXISTS agent_harness CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'harness'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';
GRANT ALL PRIVILEGES ON agent_harness.* TO 'harness'@'localhost';
FLUSH PRIVILEGES;
SQL
  echo "    已创建 MySQL 用户 harness（密码见 MYSQL_PASSWORD 环境变量，默认 CHANGE_ME_STRONG_PASSWORD，请改）"
fi

echo "==> [3/6] Python 虚拟环境 + 依赖"
cd "$APP_DIR/backend"
python3.12 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install pymysql   # requirements.txt 里没有，但 MYSQL_URL 用 mysql+pymysql 驱动，必须装

echo "==> [4/6] 生成 .env"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    已生成 backend/.env —— 必须编辑：MYSQL_URL / DEEPSEEK_API_KEY / APP_ENV / SANDBOX_ENABLED，见 README-deploy.md"
else
  echo "    .env 已存在，跳过"
fi

mkdir -p "$APP_DIR/workspace" "$APP_DIR/backend/data"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "==> [5/6] systemd 服务"
sed "s|__APP_USER__|$APP_USER|g; s|__APP_DIR__|$APP_DIR|g" \
  "$APP_DIR/deploy/agent-harness.service" > /etc/systemd/system/agent-harness.service
systemctl daemon-reload
systemctl enable --now agent-harness
sleep 3
systemctl status agent-harness --no-pager | head -15

echo "==> [6/6] Nginx 站点"
ln -sf "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/agent-harness
ln -sf /etc/nginx/sites-available/agent-harness /etc/nginx/sites-enabled/agent-harness
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo ""
echo "=========================================================="
echo " 部署完成。本机验证："
echo "   curl http://127.0.0.1/health      # 应返回 {\"status\":\"ok\",...}"
echo " 公网验证："
echo "   curl http://<公网IP>/health"
echo " 别忘了：云控制台安全组入方向开放 80（和 443）；编辑 .env 后 systemctl restart agent-harness"
echo "=========================================================="
