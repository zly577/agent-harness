# Agent Harness 部署到 Linux 云服务器・操作手册

> 适用：Ubuntu 24.04 / Debian 12 云服务器。技术栈：FastAPI + Vue3 + MySQL 8.0 + SQLite + Nginx + systemd。

## 0. 前置



* 一台有公网 IP 的 Linux 云服务器（阿里云 / 腾讯云 / 华为云等），系统建议 Ubuntu 24.04

* 一个域名（可选；没有域名直接用 `http://公网IP` 访问）

* 本机（Windows）已装好 SSH 客户端（OpenSSH 自带，`ssh` / `scp` 命令直接可用）

## 1. 本机打包上传（Windows PowerShell）



```
cd D:\Deepseek\\\_Harness

\\# 打包（排除 node\\\_modules/.env/.git/workspace/backend 本地数据；保留 frontend/dist 构建产物，服务器免装 Node）

tar -czf agent-harness.tar.gz --exclude="agent-harness/node\\\_modules" --exclude="agent-harness/.git" \\\`

\&#x20; --exclude="agent-harness/.env" --exclude="agent-harness/backend/.env" \\\`

\&#x20; --exclude="agent-harness/workspace" --exclude="agent-harness/backend/data" \\\`

\&#x20; --exclude="agent-harness/.idea" --exclude="agent-harness/frontend/node\\\_modules" agent-harness

\\# 上传到服务器

scp agent-harness.tar.gz root@39.105.158.228:/tmp/

\\# 服务器上解压

ssh root@39.105.158.228 "sudo mkdir -p /opt/agent-harness && sudo tar -xzf /tmp/agent-harness.tar.gz -C /opt/ && sudo chown -R \\\$USER:\\\$USER /opt/agent-harness"
```

> 也可以用 git：先把仓库推到 GitHub/Gitee 私有仓库，服务器上
> `git clone`
> （注意
> `.env`
> 和
> `workspace/`
> 已在 .gitignore 中，不会上传）。

## 2. 服务器一键部署



```
sudo bash /opt/agent-harness/deploy/deploy.sh

\\# 若报 /bin/bash^M 之类错误：sed -i 's/\r\\\$//' /opt/agent-harness/deploy/deploy.sh 后再执行
```

脚本自动完成：装 Python 3.12 / MySQL 8.0 / Nginx → 建库建用户 → venv 装依赖 → 生成 `.env` → 注册 systemd 服务 → 配置 Nginx。

## 3. 编辑 backend/.env（必做）



```
sudo -u \\\$(whoami) nano /opt/agent-harness/backend/.env
```



| 配置项                      | 改成                                                                           |
| ------------------------ | ---------------------------------------------------------------------------- |
| `MYSQL_URL`              | `mysql+pymysql://harness:你的强密码@127.0.0.1:3306/agent_harness?charset=utf8mb4` |
| `DEEPSEEK_API_KEY`       | 你的 DeepSeek key                                                              |
| `APP_ENV`                | `production`                                                                 |
| `HARNESS_MACHINE_ACCESS` | 保持 `false`（**绝不能开**，否则 AI 可读写服务器任意磁盘）                                        |
| `SANDBOX_ENABLED`        | 暂不部署沙箱就设 `false`；要 `execute` 可用再部署 OpenSandbox 服务并改回 `true`                  |
| `HITL_ENABLED`           | 保持 `true`（文件写操作保留人工审批）                                                       |

改完重启：`sudo systemctl restart agent-harness`

## 4. 公网访问三件事



1. **安全组**：云厂商控制台 → 该服务器安全组 → 入方向放行 `80`（HTTP）、`443`（HTTPS，可选）、`22`（SSH 保持）。**不要**放行 `8000` / `3306`。

2. **域名解析**（可选）：DNS 加一条 A 记录指向服务器公网 IP；没有域名就访问 `http://公网IP`。

3. **HTTPS**（推荐）：`sudo apt install -y certbot python3-certbot-nginx && sudo certbot --nginx -d 你的域名`，自动签发并续期。

## 5. 验证



```
curl http://127.0.0.1/health   # 服务器本机

curl http://公网IP/health      # 公网
```

浏览器打开 `http://公网IP` 看到前端页面即成功。

## 安全提醒（重要）



* 这个系统能让 AI **读写服务器文件**（workspace）并（启用沙箱后）**执行代码**，公网暴露风险高：


  * 强烈建议开启 Nginx Basic Auth（见 `deploy/nginx.conf` 注释），或后续给前端加登录；

  * MySQL 默认只监听 127.0.0.1，不要改；root 用强密码；

  * 只开放必要的端口；SSH 建议改密钥登录。

* 前端 `/api/` 反代已按 SSE 关闭缓冲，对话流式输出正常。

## 常见问题



* `/health`**&#x20;通但页面 404**：确认 Nginx root 指向 `frontend/dist`，且 dist 已打进包（打包命令没排除它）。

* **对话报 MySQL 错**：确认 MySQL 8.0 已启动、`agent_harness` 库存在、.env 里 `MYSQL_URL` 的用户密码正确；测试 `mysql -uharness -p -e "use agent_harness"`。

* **execute 提示未启用**：没部署 OpenSandbox 服务，属正常降级；文件工具不受影响。