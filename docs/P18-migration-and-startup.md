# P18 迁移执行说明与启动步骤

> 适用版本：P17 → P18（用户认证与多租户 + 多供应商 LLM）
> 本次升级已经在你的环境上实际执行过一遍，本文是**可重复**的步骤与踩坑记录。

## 一、这次升级改了什么（需要你知情的三件事）

| 变化 | 影响 |
|---|---|
| 所有 `/api/v1/*` 业务接口改为**默认拒绝** | 必须先登录；前端不再能用旧版本直接连 |
| 工作区从 `<repo>/workspace/` 改为 `<repo>/workspace/<user_id>/` | 旧文件会被**自动搬迁**（含备份） |
| 模型来源从 `.env` 唯一配置改为**供应商表** | **每个用户必须自己配模型**（右上角「模型」）；不配就没有模型可用。老版本若已有"系统级"供应商，会在启动时**自动收编**成引导管理员的私有供应商，密钥不变 |

## 二、从零启动（全新部署）

```bash
# 0. 依赖（Python 3.12，conda 环境 agent_env）
cd backend
C:/Users/Administrator/anaconda3/envs/agent_env/python.exe -m pip install -r requirements.txt

# 1. 配置：复制模板后填最少三项
cp .env.example .env
#   必需：MYSQL_URL、DEEPSEEK_API_KEY、JWT_SECRET、FERNET_KEY
#   生成密钥：
#     python -c "import secrets; print(secrets.token_urlsafe(48))"
#     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   （开发环境这两项留空也能跑：会自动落到 backend/data/.dev_secrets.json 并长期复用）

# 2. 建库（MySQL 只需存在空库，表由迁移创建）
mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS agent_harness CHARACTER SET utf8mb4;"

# 3. 启动（lifespan 里会自动 alembic upgrade head + 建引导管理员）
python -m uvicorn app.main:app --reload --reload-exclude "data/*" --reload-exclude "_probe/*"

# 4. 前端
cd ../frontend && npm install && npm run dev
```

启动后：
- 第一次登录用 `.env` 里的 `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`
  （未配置时会用 `admin@local` + 默认口令并打 WARNING —— **对外部署必须显式配置**）；
- 登录后到「模型 → 模型供应商」确认系统级供应商已就绪（点「测试」应返回"连接成功"）。

## 三、从旧版本（P17）升级

> ⚠️ **顺序很重要**：备份必须抢在改代码之前。本次我漏了这一步 —— 因为后端是
> `--reload` 起的，改完代码它立刻自动重启并执行了迁移。所幸那次迁移是纯加列，
> 数据无损（已核对行数）。**请按下面的顺序来。**

```bash
# 1) 先停掉后端（关键：避免迁移与运行中的进程互相干扰）
#    在跑 uvicorn 的那个窗口按 Ctrl+C

# 2) 备份数据库
"/c/Program Files/MySQL/MySQL Server 8.0/bin/mysqldump.exe" -u root -p \
  --single-transaction --routines --events agent_harness > agent_harness_backup_$(date +%Y%m%d_%H%M%S).sql

# 3) 拉取新代码 / 更新依赖
cd backend && pip install -r requirements.txt      # 新增 alembic

# 4) 补 .env（老 .env 里没有这几项）
#    JWT_SECRET / FERNET_KEY / ACCESS_TOKEN_EXPIRE_MINUTES
#    ALLOW_REGISTRATION / BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD
#    CORS_ORIGINS
#    详见 .env.example 的「P18」段

# 5) 执行迁移（也可以在启动时自动执行，见下）
python -m alembic upgrade head
#    它会：
#      - 建 users / llm_providers
#      - 给 sessions 加 user_id / provider_id，给两张日志表加 user_id
#      - 幂等：已存在的表/列会跳过（老库、半新库、全新库都能收敛）

# 6) 启动服务 —— lifespan 会做三件收尾事：
#    a) 建引导管理员（users 表为空时）
#    b) 把存量会话/日志归属到它（之后每次启动都会兜底回填无主行）
#    c) 搬迁旧工作区 + 把遗留的"系统级供应商"收编成引导管理员的私有供应商
python -m uvicorn app.main:app --reload --reload-exclude "data/*" --reload-exclude "_probe/*"
```

### 迁移做了什么（`0001_baseline` + `0002_auth_multitenant`）

- `0001`：P2/P5 那三张表——**已存在的库只记版本号，一张表都不建**；全新库才建。
- `0002`：建 `users` / `llm_providers`，给三张老表加 `user_id`（+索引）与 `sessions.provider_id`。
- **刻意不做数据操作**（建管理员、回填归属）——那部分放在启动引导里，
  一是迁移不该 import 应用代码，二是**每次启动都能自愈**过渡期新产生的无主行。

### 为什么 `sessions.user_id` 允许 NULL

老库加 `NOT NULL` 直接执行不过去；更关键的是回填之后任何旧代码路径再写无主行都会
让整轮对话 500。所以策略是"DDL 允许 NULL + 应用层强制 + 启动回填"。
安全性不受影响：查询一律 `WHERE user_id = <我>`，NULL 行**谁都匹配不到**（fail-closed，
最坏是看不见，绝不会被看见）。本次实测回填后会话无主行为 0 条。

## 四、回滚

```bash
# 结构回滚（会删掉 users / llm_providers 两张表 —— 口令与已加密的供应商密钥一起没）
cd backend && python -m alembic downgrade 0001_baseline

# 数据回滚
mysql -uroot -p agent_harness < agent_harness_backup_<时间戳>.sql

# 工作区回滚（无损：搬迁前做过整目录备份）
#   删掉 workspace/<user_id>/，把备份目录改名回 workspace/
#   备份位置：<repo>/workspace_backups/workspace_backup_<时间戳>/
```

## 五、`--reload-exclude` 不是可选项（血泪）

`uvicorn --reload` 默认监听整个 `backend/`。**任何运行时写在 `backend/` 里的文件
都会触发一次 reload**，而 reload 又可能再写文件 → 无限重启循环。

实测：工作区备份目录放在 `backend/data/` 下时，4 分钟生成 **41 份备份**、后端一直重启，
现象是"端口 LISTENING 但请求全部失败/500" —— 看着就是服务挂了。

所以：**运行时可写目录一律排除**（`data/*`、`_probe/*`、`logs/*`）。
`一键启动.bat` 已经加上。新增任何"运行时写盘"的目录时，记得同步补进这个参数。

## 六、模型供应商怎么配（P18 新能力）

「模型 → 模型供应商」里新增，两种协议：

| 协议 | 适用 |
|---|---|
| **OpenAI 兼容** | OpenAI、DeepSeek、Kimi/Moonshot、通义（兼容模式）、智谱（兼容模式）、OpenRouter、硅基流动、vLLM、Ollama、LM Studio |
| **Anthropic** | Claude 官方及 Anthropic 协议兼容实现 |

字段：`base_url`（可空 = 官方默认地址）、`api_key`（**加密落库**，界面只显示掩码）、
`model`、`parameters`（temperature / timeout / max_tokens）。

### 隔离与选择规则（2026-09-18 收紧）

- **每个用户只能看到、也只能使用自己配的供应商**。没有配置 → 发消息（以及读历史、
  看审批）都会得到明确的 400 + 中文提示，引导你去「模型」里加一条。
- 顶栏选择器**没有「跟随默认」这一项** —— 必须显式选中一个模型才能发消息；
  未选中时选择器会高亮提示。
- 同一个用户配了多条时，标了"默认"的那条会被自动选中；没有默认就用最早创建的那条。
- 会话可以各自绑定模型；绑定的那条被删/停用后，**自动回退**到你自己的默认。
- **必须支持 function/tool calling**：智能体的文件读写、`task` 委派全靠工具调用。

> 早期版本支持"系统级共享供应商"（所有用户共用一个 key）。该机制已取消 ——
> 表结构里 `user_id IS NULL` 的能力还留着，将来要恢复共享只需改
> `app/agent/providers.py` 的解析顺序与 `api/routes/llm.py` 的可见范围两处。
- 点「测试连接」会发一条最小请求，并把原因说成人话（401 密钥无效 / 404 地址或模型名不对 /
  连不上 / 限流）。

### ⚠️ 三家的参数名不同（写错不会报错）

`ChatAnthropic` 用的是 `anthropic_api_key` / `anthropic_api_url` / `default_request_timeout`，
**没有** `api_key`/`base_url`/`timeout`；三家的 `extra` 都是 `ignore`，写错只会静默地
"用默认地址 + 空密钥"去请求，然后回一个看不懂的 401。另外 **Anthropic 的 `max_tokens`
是必填**（没配时自动兜 4096）。这些都在 `app/agent/model.py` 里按规范名处理并有测试守住。

## 七、跑测试与验证

```bash
# 后端（业务库连的是 .env 里的 MYSQL_URL，测试账号 @p18.test 会自动清理）
cd backend && python -m pytest -q

# 前端浏览器验证（需要 Edge 无头 + CDP；凭证走环境变量，不写进脚本）
export AH_EMAIL=admin@local AH_PASSWORD=<口令>
node _verify/verify_auth_login.mjs 9222 http://127.0.0.1:5173/ _verify   # 登录态 15 项
node _verify/verify_providers.mjs  9222 http://127.0.0.1:5173/ _verify   # 供应商界面 20 项
node _verify/verify_tool_labels.mjs 9222 http://127.0.0.1:5173/ _verify http://127.0.0.1:8000
node _verify/verify_file_delete.mjs 9222 http://127.0.0.1:5173/ _verify http://127.0.0.1:8000
```

> 旧的验证脚本（`verify_hitl_toggle` / `verify_multi_approval` / `verify_run_persist` /
> `verify_subagent_ui` / `verify_scroll_follow` / `verify_browser`）也已补上登录步骤与
> 按用户分区的 localStorage key。其中需要"挂起中断会话"的那几个仍需先准备相应会话。

## 八、已知遗留（不属于本次需求，但要知道）

1. **磁盘上的密钥文件**：`backend/data/.dev_secrets.json`（开发环境自动生成的
   `JWT_SECRET`/`FERNET_KEY`）。对外部署请改为显式配置 `.env` 并备份 `FERNET_KEY` ——
   换掉它会导致所有已保存的供应商密钥**永久解不开**（接口会提示"请重新填写"）。
2. **checkpoint 数据库未加密**：`backend/data/harness.sqlite` 里有所有用户的消息历史，
   靠文件系统权限保护。隔离在业务层强制（`thread_id == session_id` 不变）。
3. **`/mnt/*` 本机磁盘挂载当前对所有用户开放**（按你的决定保留）。
   它绕过工作区隔离 —— 对外部署前建议改为"仅管理员"，只要改 `HARNESS_MACHINE_ACCESS`
   的判定点。
4. **无 refresh token / 无 token 吊销**：access token 过期需重新登录；改口令不会让
   已签发的 token 失效（无状态 JWT 的固有语义，要立刻踢下线需引入 token 版本号）。
5. **无登录限流**：暴力尝试口令不会被拦。自用/内网可接受，对外需要加。
