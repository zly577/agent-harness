#!/usr/bin/env bash
# 修复 Agent Harness 服务器：MySQL harness 密码 + .env + 重启 + 健康检查
# 用法：scp 到服务器后执行  bash /tmp/fix-server.sh
# 如果你在 deploy.sh 里设过自定义 MySQL 密码： MYSQL_PASSWORD=你的密码 bash /tmp/fix-server.sh
set -euo pipefail

# Windows CRLF 自防护（无害）
sed -i 's/\r$//' "$0" 2>/dev/null || true

MYSQL_PASSWORD="${MYSQL_PASSWORD:-CHANGE_ME_STRONG_PASSWORD}"
ENV_FILE=/opt/agent-harness/backend/.env

echo "==> [1/5] 重置 MySQL harness 用户密码"
mysql -u root <<SQL
ALTER USER 'harness'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';
FLUSH PRIVILEGES;
SQL

echo "==> [2/5] 修改 .env"
sed -i "s|^MYSQL_URL=.*|MYSQL_URL=mysql+pymysql://harness:${MYSQL_PASSWORD}@127.0.0.1:3306/agent_harness?charset=utf8mb4|" "$ENV_FILE"
sed -i 's|^APP_ENV=.*|APP_ENV=production|' "$ENV_FILE"
sed -i 's|^SANDBOX_ENABLED=.*|SANDBOX_ENABLED=false|' "$ENV_FILE"
grep -E '^(MYSQL_URL|APP_ENV|SANDBOX_ENABLED)=' "$ENV_FILE"

echo "==> [3/5] 重启服务"
systemctl restart agent-harness
sleep 5

echo "==> [4/5] 健康检查"
curl -s http://127.0.0.1:8000/health
echo ""

echo "==> [5/5] 提示"
echo "若上方输出 {\"status\":\"ok\"...} 即成功。"
echo "还差一步：把 backend/.env 里的 DEEPSEEK_API_KEY 换成你的 key（聊天要用），"
echo "改法：ssh 上去后执行  nano /opt/agent-harness/backend/.env  （或用 vi）"
