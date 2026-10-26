"""FastAPI 应用入口（骨架 + 健康检查；启动建表；迁移 + 认证）。

## 三处变化

1. **建表方式从 `create_all` 换成 `alembic upgrade head`。**
   `create_all` 只能"建缺失的表"，**加不了列** —— 本次要给三张老表加 `user_id`，
   它无能为力。迁移入口见 `_run_migrations()`。

2. **CORS 从 `allow_origins=["*"]` 收紧为白名单**（`settings.cors_origin_list`）。
   认证走 Bearer token 而不是 Cookie，所以不开 `allow_credentials`。

3. **auth router 单独挂载**，不经过 `api_router`（后者带全局登录依赖）。
   挂错一处就变成"登录接口要求先登录"。
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.providers import ProviderNotConfigured
from app.api.routes import api_router
from app.api.routes import auth as auth_routes
from app.api.routes.sessions import picker_callback_router
from app.api.routes.files import desktop_callback_router
from app.core.config import settings

logger = logging.getLogger(__name__)

#: backend/ 目录（用于定位 alembic.ini）
_BACKEND_DIR = Path(__file__).resolve().parent.parent
#: 仓库根目录（backend/ 的上一级），日志放这里而不是 backend/ 里 ——
#: `--reload-dir app` 只监听 app/，但"运行时往 backend/ 里写文件触发 reload 死循环"
#: 是踩过两次的坑，日志这类高频写入的东西绝不能落在 backend/ 内。
_REPO_ROOT = _BACKEND_DIR.parent


def _setup_file_logging() -> None:
    """给 root logger 挂一个滚动文件 handler，把日志落到 `logs/backend.log`。

    ## 为什么必须有这个

    后端历来靠 `cmd /k` 窗口里那一串 stdout 活着，窗口一关日志就没了。而真正要查的
    恰恰是"卡死 / 报错"那类需要事后追溯的信息（2026-09-18 两次查因都因为日志没落盘，
    只能靠反推 checkpoint 和 DB）。像 langchain_openai 的 `StreamChunkTimeoutError`
    （流式块间超时）也是打到 logging 的 WARNING，不落盘就永远看不见。

    挂到 **root** logger 上：各模块的 `[mcp]`/`[web_search]`/`[runner]`、以及第三方 SDK
    的日志都向上传播到 root，一个 handler 全接住。

    ## ⚠️ 但 uvicorn 自己的日志**不会**传播到 root（2026-10-09 实测修正）

    上面的"一个 handler 全接住"只对**应用与第三方库**成立。uvicorn 的
    `LOGGING_CONFIG` 把 `uvicorn` 与 `uvicorn.access` 都设成了 `propagate: False`
    （`uvicorn.error` 自己没 handler，靠向上冒到 `uvicorn` 后就停住），所以：

    - 启动/关闭信息（"Started server process" / "Uvicorn running" / "Application
      startup complete"）—— 不在文件里；
    - **每个请求的访问日志** —— 不在文件里（于是"这个请求到底有没有到后端"无从判断）；
    - 🔥 **未捕获异常的 traceback** —— 不在文件里。FastAPI 的
      `ServerErrorMiddleware` 是把 traceback 打给 `uvicorn.error` 的，
      所以"某个接口报 500"这件事在日志里**一个字都没有**。

    2026-10-09 排查"点停止报 HTTP 500"时就卡在这里：文件里既没有那条 POST /stop，
    也没有任何 traceback，只能靠 `netstat`/进程表反推。所以下面显式把这几个
    logger 接到同一个 handler 上（**只接 `uvicorn.error` / `uvicorn.access`，
    不接父级 `uvicorn`** —— 接父级会让同一条记录被打印两遍，因为
    `uvicorn.error` 的 propagate 为真、会冒到父级再走一次 handler）。
    """
    log_dir = _REPO_ROOT / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 建不了目录就别建，日志照常走 stdout
        return

    handler = RotatingFileHandler(
        log_dir / "backend.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB 滚动
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    # 文件里记录 INFO 及以上即可；stdout 的级别交给 uvicorn 自己控制。
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)

    for name in ("uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        if handler not in lg.handlers:
            lg.addHandler(handler)
        # 这几个 logger 可能是 uvicorn 配好的（level=INFO），也可能是 import 顺序
        # 反过来时还没配 —— 两种情况下都确保不会因为级别过低而丢掉记录。
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


_setup_file_logging()


def _run_migrations() -> None:
    """把业务库升到最新版本（等价于 `alembic upgrade head`）。

    迁移脚本是**幂等**的（每一步都先查现状再看要不要做），所以重复启动安全；
    老库第一次跑只会补上缺的列与表。

    ⚠️ 多 worker 部署时不要依赖这里迁移：`uvicorn --workers 4` 会有 4 个进程
    同时开跑。本项目是单进程（`--reload` 的 reloader 只有 worker 跑 lifespan），
    对外部署建议改成"发布流程里先执行 `alembic upgrade head`"。
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    # 别让 alembic.ini 的 [loggers] 段覆盖 uvicorn / 应用的日志配置
    # （alembic/env.py 会看这个标记决定要不要调 fileConfig）
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动：迁移 → 引导管理员 + 存量归属回填 ----
    try:
        _run_migrations()
        logger.info("数据库已迁移到最新版本")
    except Exception as e:  # noqa: BLE001
        # 迁移失败**绝不**降级为"先跑起来再说"：表结构不对会让之后每个请求
        # 以各种离奇方式失败（列不存在、500），排查成本远高于现在起不来。
        logger.exception("数据库迁移失败，拒绝启动")
        raise RuntimeError(f"数据库迁移失败：{type(e).__name__}: {e}") from e

    from app.db.bootstrap import run_bootstrap

    run_bootstrap()

    try:
        yield
    finally:
        from app.agent.runner import run_manager
        await run_manager.aclose()
        await _close_agent_storage()


async def _close_agent_storage() -> None:
    # ---- 收尾：关掉 checkpointer/store 的全局 SQLite 连接 ----
    # 不关的话解释器退出会挂住（实测 30s+ 退不出去），于是 `uvicorn --reload`
    # 换代码时旧 worker 关不掉：父进程还占着端口、连接也能建，但没人应答 HTTP，
    # 看起来就是"服务挂了"。加超时兜底，绝不让收尾把关闭流程拖住。
    from app.agent import context as agent_context

    try:
        await asyncio.wait_for(agent_context.aclose(), timeout=5)
    except Exception as e:  # noqa: BLE001
        logger.warning("[shutdown] 关闭存储连接失败（忽略，继续退出）: %s", e)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# CORS：开发期 = Vite 的 127.0.0.1:5173 / localhost:5173（配置项 CORS_ORIGINS）。
# 原来这里是 allow_origins=["*"] —— 配合 localStorage 里的 token，等于任何
# 网页都能带着用户的凭证调这套 API。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 认证路由（注册/登录/me/改口令）：**不经过** api_router
app.include_router(auth_routes.router)

# Windows directory selection returns only a short-lived ticket result.
# Creating or polling a workspace stays on the authenticated business router.
app.include_router(picker_callback_router)
app.include_router(desktop_callback_router)

# 业务路由：全部要求已登录（默认拒绝，见 app/api/routes/__init__.py）
app.include_router(api_router)


@app.exception_handler(ProviderNotConfigured)
async def _provider_not_configured_handler(request, exc: ProviderNotConfigured):
    """"你还没配置模型"统一转 **400**（而不是 500）。

    为什么放在全局而不是逐个路由：
    - 它是**用户自己就能解决**的前置条件，不是服务端故障 —— 500 会让前端提示
      "服务端错误"，把用户引向完全错误的方向；
    - 会碰到它的入口不止一个（发消息、读历史、看审批），逐个写必然漏掉某一个，
      而漏掉的那个就会表现为"界面一片红、说是服务端错误"。
    """
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/")
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
    }
