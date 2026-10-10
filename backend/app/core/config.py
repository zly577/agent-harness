"""全局配置：从 .env / 环境变量读取（pydantic-settings）。

P2 起新增：
- MYSQL_URL：业务数据持久化（MySQL 8.0）
- SQLITE_PATH：langgraph checkpoint/store 消息历史（SQLite 单文件）

P18 起新增：
- JWT_SECRET / JWT_ALGORITHM / ACCESS_TOKEN_EXPIRE_MINUTES：用户认证
- FERNET_KEY：供应商 api_key 的对称加密主密钥
- ALLOW_REGISTRATION / BOOTSTRAP_ADMIN_*：注册开关与引导管理员
- CORS_ORIGINS：跨域来源白名单（原来写死 `["*"]`）

MySQL 迁移到 PostgreSQL 时只需改 MYSQL_URL 并替换驱动，对上层无耦合。
"""
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend 目录的绝对路径（用于默认 SQLite 位置）
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ===== 应用 =====
    app_name: str = "Agent Harness"
    app_version: str = "0.1.0"
    app_env: str = "development"

    # ===== DeepSeek / LLM =====
    # P18 起语义变化：这组配置不再是"唯一的模型来源"，而是
    # **系统级供应商的引导配置** —— 启动时若库里没有任何系统级供应商
    # （llm_providers.user_id IS NULL），就用这里的值自动建一条，
    # 保证老部署升级后开箱可用。用户自己的供应商配置在「设置 → 模型供应商」里。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # ===== LLM 流式调用块间超时（秒）=====
    # 关键：`request_timeout`（= providers.parameters.timeout，默认 60）只管
    # 「连接 + 首字节」，**管不住流式过程**。推理模型（deepseek-flash/reasoner）在
    # 推理阶段会持续吐 reasoning_content，若中途卡住（对端静默断开/挂起），
    # `async for chunk` 会永久 await 下一个永远不来的 chunk —— 这就是 2026-09-18
    # "重新做任务"卡死的根因：model 节点永远不返回、前端 SSE 一直转圈、worker 却
    # 无任何网络活动。
    # 设了这个值后，每个 chunk 的等待都套 `asyncio.wait_for`，超时抛
    # StreamChunkTimeoutError，model 节点能及时报错而不是无限挂起。
    # 0 = 关闭（不推荐）。
    llm_stream_chunk_timeout_seconds: float = 300.0

    # ===== 工具参数生成期的进度心跳（秒）=====
    # 关键：模型把大段内容塞进**工具参数**时（不是正文 content），`tool_call` 事件要等参数
    # JSON **拼完整**才发得出来。实测一个 `write_file` 的 content ≈ 82KB、耗时 **468 秒**，
    # 这段时间前端**一个事件都收不到**、日志也一行不出 ⇒ 用户看到的就是"卡死了"，而进程
    # 其实在全速输出（≈69 token/s）。这里补一条**节流**心跳，让"它还在写"可见。
    # 两段节流：① 先等 `after` 秒 —— 正常的小工具调用早就跑完了，不该被打扰；
    #           ② 之后每 `interval` 秒一条 —— 8 分钟最多约 240 条，回放缓冲扛得住。
    # 注意：它只能覆盖「模型在慢慢吐」，**覆盖不了「对端静默挂起」**（那时没有 chunk 到达，
    # 自然也不会有心跳）；后者靠上面的 `llm_stream_chunk_timeout_seconds` 兜。
    tool_args_progress_enabled: bool = True
    tool_args_progress_after_seconds: float = 3.0
    tool_args_progress_interval_seconds: float = 2.0

    # 联网搜索（tavily_search 工具）。留空时该工具会快速返回"缺少 Key"的提示，
    # 而不是让子智能体反复重试到熔断。
    tavily_api_key: str = ""

    # ===== P2：持久化 =====
    # 业务数据（MySQL）。⚠️ 不要在代码里写真实口令：只保留一个"本机开发用的空模板"，
    # 实际连接串必须来自环境变量 MYSQL_URL（见 backend/.env，已 gitignore）。
    mysql_url: str = "mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/agent_harness?charset=utf8mb4"
    # langgraph 消息历史（SQLite 单文件）
    sqlite_path: str = str(_BACKEND_DIR / "data" / "harness.sqlite")

    # ===== P2：上下文管理 =====
    # 触发截断/摘要的消息条数阈值（超过则处理）。
    # 注意：阈值按「消息条数」而非 token 计，一条带工具调用的用户轮次通常产生
    # 5~8 条消息（AI 工具调用 + ToolMessage），阈值过小会导致几乎每轮都触发压缩。
    context_max_messages: int = 80
    # 摘要后保留的最近消息条数（约 3~5 轮工具调用）；建议为 max 的 1/3 左右。
    context_keep_messages: int = 24

    # ===== P5：工具调用次数上限（ToolCallLimitMiddleware，库自带）=====
    # 语义提醒：统计的是【调用次数】（成功也算），不是失败次数。
    #   tool_call_thread_limit: 该工具在每个会话（thread）允许的总调用次数；0 = 关闭（默认关）
    #   tool_call_run_limit   : 该工具在每次运行（一轮对话）允许的调用次数；0 或留空 = 不设
    #   tool_call_limit_tool  : 只限制这一个工具；留空 = 限制所有工具合计
    #   tool_call_limit_exit_behavior: continue(降级，默认) / error(抛异常) / end(立刻结束)
    tool_call_thread_limit: int = 0
    tool_call_run_limit: int = 0
    tool_call_limit_tool: str = "tavily_search"
    tool_call_limit_exit_behavior: str = "continue"

    # ===== P5：工具【连续失败】熔断降级（自研中间件，对所有工具各自生效）=====
    # 语义：每个工具独立计数，连续失败达到阈值 → 进入冷却期（调用被直接跳过、不执行），
    #       并把"换方法"提示回给模型；冷却期过后自动放一次"探测调用"：
    #       探测成功 → 该工具完全恢复；失败 → 重新进入冷却期。
    #   failure_limit_threshold : 连续失败多少次后进入冷却；0 = 关闭
    #   failure_limit_cooldown_seconds: 冷却时长（秒）。工具"挂一会儿又好了"就靠它自愈
    #   failure_limit_reset_tool: 模型可主动调用的"手动恢复"工具名；留空 = 不提供
    failure_limit_threshold: int = 5
    failure_limit_cooldown_seconds: int = 60
    failure_limit_reset_tool: str = "reset_tool_breakers"
    # 兼容旧配置名（只统计某个工具）；留空 = 所有工具各自独立熔断（推荐）
    failure_limit_tool: str = ""

    # ===== P7/P19：MCP =====
    # 总开关：是否加载外部 MCP 工具。关掉时数据库里的配置都留着，只是不连。
    mcp_enabled: bool = False
    # 单个 MCP server 的连接超时（秒）。
    # ⚠️ 这不是"越小越好"的调参项：build_mcp_tools 跑在 get_agent() 里、也就是在
    # **请求路径上**，一个不可达的端点会把"建 agent"整段卡住。所以必须有上限。
    mcp_connect_timeout_seconds: float = 15.0
    # 单个 MCP 工具【执行】超时（秒）。连接成功 ≠ 调用会返回：像 bing_search 这类
    # 服务端收到 tools/call 后还要自己再跑一次真实搜索，迟迟不吐结果时，没有这个
    # 上限就会让 agent 的 tools 节点永久挂起（前端 SSE 一起卡死）。超时以 `Error:`
    # 开头返回，让熔断器识别并冷却。0 = 关闭（不推荐）。
    mcp_tool_call_timeout_seconds: float = 120.0

    # ===== P8：人工介入（HITL）热插拔默认值 =====
    # True  = 访问本机真实文件（/mnt/**，见 HARNESS_MACHINE_ACCESS）会触发人工审批中断，
    #         需点"同意"才执行；工作区 /workspace/** 不受影响（沙箱内自由读写）。
    # False = 全程自动执行，不产生任何审批中断
    # 运行期可通过 PUT /api/v1/hitl/config 热切换（改这里是改重启后的默认值）
    hitl_enabled: bool = True

    # ===== 权限模式（P21：沙箱内 / 完全访问）=====
    # 运行期可通过 PUT /api/v1/access/config 热切换；这里是**重启后的默认值**。
    # - "sandbox"（默认）：文件读写落本地工作区，execute 进 OpenSandbox 容器；
    #   访问本机真实文件（/mnt/**）需要人工审批（见 hitl_enabled）。
    # - "full"：完全访问 —— execute 直接在本机 shell 里跑（deepagents LocalShellBackend），
    #   本机文件读写不再需要审批。**没有任何隔离**，只应在你信任这台机器上跑的东西时打开。
    # 默认值刻意是 sandbox：完全访问必须由人显式选择，重启后自动回落到安全档。
    harness_access_mode: str = "sandbox"

    # ===== P11：OpenSandbox 远程执行沙箱 =====
    # 是否启用 OpenSandbox 作为 execute 的执行后端
    # True  = 文件读写走本地磁盘（FilesystemBackend），execute 进 OpenSandbox 容器
    # False = 完全不碰沙箱；execute 工具仍存在但返回「未启用」提示
    sandbox_enabled: bool = True
    # 执行前后是否做「本地 ↔ 沙箱」文件同步（推送改动进容器 / 拉回容器产出）
    # 关闭后容器里读不到本地文件、产出也不回本地磁盘，仅适合纯命令场景
    sandbox_sync_enabled: bool = True
    # OpenSandbox 服务地址：domain 不带协议前缀，协议单独用 protocol
    sandbox_domain: str = "127.0.0.1:8080"
    sandbox_protocol: str = "http"
    sandbox_api_key: str = ""
    # 【远端部署必看】客户端能否直连沙箱容器？
    # - False：SDK 会向服务端要"容器直连地址"，然后自己去连那个地址。
    #          要求客户端网络能直达容器 IP（同机 / 同一 docker 网络 / 端口映射可达）。
    # - True ：所有 execd 请求（执行命令、传文件）都**经过 OpenSandbox 服务端代理**。
    #          客户端只需要能访问 SANDBOX_DOMAIN 一个地址即可 —— 远程 Linux 部署、
    #          容器在 docker 私有网段、客户端无法直达容器时，必须用 True。
    sandbox_use_server_proxy: bool = True
    # 额外请求头（JSON 字符串）。SDK 在 Linux/macOS 会自动带 OPEN-SANDBOX-CLIENT-IP，
    # Windows 上探测不到（getifaddrs 不存在），服务端若依赖该头判定客户端网络，可在这里手填：
    # SANDBOX_HEADERS={"OPEN-SANDBOX-CLIENT-IP": "192.168.1.50"}
    sandbox_headers: str = ""
    # 容器镜像（必须自带 python3 + 标准 coreutils）
    sandbox_image: str = "python:3.11"
    # 资源与生命周期
    sandbox_cpu: str = "1"
    sandbox_memory: str = "2Gi"
    sandbox_ttl_minutes: int = 60  # 单沙箱最长存活，到期服务端自动回收
    sandbox_ready_timeout_seconds: int = 60
    sandbox_default_timeout_seconds: int = 120  # 单条 execute 默认超时
    # 创建沙箱的 HTTP 请求超时。首次创建要拉镜像（python:3.11 + opensandbox/execd），
    # 30 秒远远不够 —— 默认给 5 分钟；镜像已在本地时创建只需几秒。
    sandbox_create_timeout_seconds: int = 300
    # 虚拟路径 → 沙箱真实路径的根（建议 /workspace，别用 /）
    sandbox_root: str = "/workspace"

    # ===== P20：工具调用意图标签（纯展示层）=====
    # 每次工具调用附带一句给用户看的说明（`_intent`），落到 tool_call 事件与
    # 任务看板的"当前步骤"上。关掉后：不追加提示词段落、不往工具 schema 里注入
    # `_intent`、事件里 intent_label / intent_source 恒为 null。
    # ⚠️ 参数剥离**不受这个开关影响**（`_intent` 永远在执行前被摘掉）——
    #    否则存档里带过该字段的旧消息会让工具收到意外参数而报错。
    intent_label_enabled: bool = True
    # 提示词与兜底文案的语言。目前是"部署级" locale（本项目默认中文用户）；
    # 提示词里另外要求模型"用用户提问的语言写"，所以单会话切语言也能跟随。
    # 将来若要做 per-request locale，只需把它作为参数传给 build_system_prompt。
    intent_locale: str = "zh"

    # ===== 子智能体（task 委派）=====
    # 单个子智能体的最长运行时间（秒）。超过后中止并把原因回给主智能体。
    # 为什么必须有：deepagents 的 task 工具是 `await subagent.ainvoke(...)`，
    # 期间主事件流完全没有该子智能体的任何事件。不设上限的话，子智能体陷入
    # 长循环或卡在某个慢工具里时，用户只能看到"一直转圈"，无法区分"在干活"和"挂了"。
    subagent_timeout_seconds: int = 900

    # ===== P12：本机磁盘访问（默认环境 = 本机电脑，只有 execute 才进沙箱）=====
    # True  = 文件工具除了工作区 /workspace 之外，还能**直接读写本机磁盘**：
    #         每个挂载点在模型侧是一个 `/mnt/<名称>/...` 目录（例如 /mnt/d/Deepseek_Harness/a.py
    #         就是 D:\Deepseek_Harness\a.py）。execute 仍然只在沙箱容器里跑。
    # False = 只有工作区可见（P11 行为）。
    harness_machine_access: bool = False
    # 挂载点定义：逗号分隔，支持 `名称=路径` 与裸路径两种写法。
    #   例：HARNESS_MACHINE_MOUNTS=d=D:/,home=C:/Users/Administrator,proj=D:/work/api
    # 留空 = 自动挂载本机所有存在的盘符（c=C:/、d=D:/ ……）。
    # 注意：挂载点默认**没有**沙箱镜像 —— 要让 execute 用到某个文件，先放进 /workspace，
    #       或打开下面的 SANDBOX_MOUNT_MACHINE_DIRS（方案 A，把挂载点只读挂进容器）。
    harness_machine_mounts: str = ""

    # ==== P12+（方案 A）：把本机挂载点**只读挂载进沙箱容器** ====
    # 要解决的故障：`/mnt/<名>/...` 只是宿主文件工具的 route，容器里看不到它 —— 于是
    #   模型 `read_file /mnt/d/a.xlsx` 成功，但紧跟着
    #   `execute("python -c \"import pandas; pandas.read_excel('/mnt/d/a.xlsx')\"")` 必然失败。
    #   而 execute 的失败返回**既不带 `Error:` 前缀、也没有 status=error**（deepagents 只把
    #   输出拼上 `[Command failed with exit code N]`），熔断器判定为"成功"而不计数
    #   ⇒ 模型反复换招空转（实测一次任务里 execute 20 次 + MCP 19 次 + read_file 12 次）。
    # True  = 把 machine_mounts() 的每个目录以**只读**方式挂载到容器内的**同一虚拟路径**
    #         `/mnt/<名>` ⇒ read_file 与 execute 看到的是同一个文件、同一个路径字符串，
    #         路径命名空间不一致的问题从根上消失，大文件也不受 32MB 同步上限约束。
    #         前提：OpenSandbox 服务端与目标文件**同机**（远端 ECS 上没有你的 D 盘）；
    #         且运行时（Docker/K8s）允许 host mount —— 不支持时创建容器会失败。
    # False = 容器里没有 /mnt（P12 原行为）。
    sandbox_mount_machine_dirs: bool = False
    # 挂载是否只读。**保持 True**：写本机文件仍然只能走宿主 FS 的 route + 人工审批；
    # 改成 False 会让容器绕过审批直接写本机文件，只在你明确接受该风险时才改。
    sandbox_mount_read_only: bool = True

    # ===== P18：认证与多租户 =====
    # JWT 签名密钥（HS256）。
    # - development：留空时自动落到 backend/data/.dev_secrets.json（首次生成后长期复用，
    #   所以重启不会把所有人登出）；该目录已在 .gitignore 里。
    # - 其它环境：**必须显式配置**，缺失直接拒绝启动（见 _check_production_secrets）。
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    # access token 有效期（分钟）。默认 24 小时；前端拿到 401 会自动回登录页。
    access_token_expire_minutes: int = 1440

    # 供应商 api_key 的对称加密主密钥（cryptography.Fernet，44 字符 base64）。
    # 生成：python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # ⚠️ 这个密钥一旦丢失/更换，库里已存的供应商密钥将无法解密（要用户重新填一遍），
    #    所以生产环境请纳入密钥管理并备份。development 下留空同 jwt_secret 的处理。
    fernet_key: str = ""

    # 是否开放自助注册。开发期 true；私有部署建议 false（用户由管理员创建）。
    allow_registration: bool = True

    # 引导管理员：迁移时若 users 表为空则创建它，并把存量会话/日志归属到它。
    # 未配置 PASSWORD 时会用固定默认值并打 WARNING（仅便于本机开箱；生产必须配）。
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_display_name: str = "管理员"

    # CORS 允许来源（逗号分隔）。开发期 = Vite 默认地址；上线改成真实前端域名。
    # 注意：用 Bearer token 而不是 Cookie，所以不需要开 allow_credentials。
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS 来源列表（过滤空项，便于在 .env 里写逗号分隔的一行）。"""
        return [o.strip() for o in (self.cors_origins or "").split(",") if o.strip()]

    @property
    def is_development(self) -> bool:
        return (self.app_env or "").strip().lower() in ("development", "dev", "local")

    @model_validator(mode="after")
    def _check_production_secrets(self) -> "Settings":
        """非开发环境必须显式配置密钥。

        为什么要在配置层拦：`jwt_secret` 缺失时如果悄悄用一个随机值，重启即全体登出；
        `fernet_key` 缺失时如果悄悄生成，会让**已加密的供应商密钥永久解不开**。
        这两种失败都是"静默的"，比启动失败危险得多 —— 所以宁可开不起来。
        """
        if not self.is_development:
            missing = [
                name
                for name, value in (("JWT_SECRET", self.jwt_secret), ("FERNET_KEY", self.fernet_key))
                if not (value or "").strip()
            ]
            if missing:
                raise ValueError(
                    f"APP_ENV={self.app_env} 下必须显式配置：{', '.join(missing)}。"
                    "生成 FERNET_KEY：python -c \"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\""
                )
        return self

@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
