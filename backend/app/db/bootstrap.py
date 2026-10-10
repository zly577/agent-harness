"""启动引导（P18）：引导管理员 + 存量数据归属回填。

## 为什么放在启动时，而不是放在 alembic 迁移里

需求原文是"迁移时创建引导管理员"。实现时改成了启动时（每次启动都跑、幂等），
两个具体理由：

1. **迁移脚本不该 import 应用代码**。创建用户要算口令哈希、要读 `.env`，
   一旦迁移依赖 `app.core.security` / `app.core.config`，以后重构它们就可能让
   这份历史迁移跑不起来（alembic 的经典坑）。
2. **回填需要能自愈**。升级是"改代码 + 迁移"两步，中间只要还有一个老进程在跑，
   它就会继续写入无归属的行（`sessions.user_id IS NULL`）。放在启动时，
   下次重启自动收干净；放在迁移里就永远只有那一刻的存量被修好。

## 幂等性

每一条都是"只处理 NULL 的，处理完就不再匹配"，所以重复执行是安全的、
而且第二次几乎是零成本（`WHERE user_id IS NULL` 走不到索引也没关系，
这两张表的规模在本项目里是几百行量级）。
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as OrmSession

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import AuditLog, Session as DbSession, ToolCallLog, User

logger = logging.getLogger(__name__)

#: 没配 BOOTSTRAP_ADMIN_PASSWORD 时用的默认口令。
#: 只在开发环境允许 —— 非开发环境配置层的校验会要求显式配置（见 config.Settings）。
_FALLBACK_ADMIN_PASSWORD = "admin12345"
_FALLBACK_ADMIN_EMAIL = "admin@local"


def _resolve_admin_credentials() -> tuple[str, str, str, bool]:
    """从配置解析引导管理员的三要素。

    Returns:
        `(email, password, display_name, used_fallback_password)`
    """
    email = (settings.bootstrap_admin_email or "").strip() or _FALLBACK_ADMIN_EMAIL
    password = (settings.bootstrap_admin_password or "").strip()
    display = (settings.bootstrap_admin_display_name or "").strip() or "管理员"

    used_fallback = False
    if not password:
        if not settings.is_development:
            # 正常走不到这里（config 的 model_validator 会先拦），留作第二道保险
            raise RuntimeError(
                "非开发环境必须配置 BOOTSTRAP_ADMIN_PASSWORD（不能用默认口令）。"
            )
        password = _FALLBACK_ADMIN_PASSWORD
        used_fallback = True
    return email, password, display, used_fallback


def ensure_bootstrap_admin(db: OrmSession) -> tuple[User | None, bool]:
    """确保存在一个可用于接收存量数据的账号。

    - `users` 表为空 → 按配置创建引导管理员；
    - 已有用户 → **不新建**，按 "配置邮箱 → 最早的 admin → 最早的 user" 顺序
      挑一个作为存量数据的归属（升级重跑时会走到这条分支）。

    Returns:
        `(user, created)`；表还不存在等异常时返回 `(None, False)`。
    """
    try:
        total = db.scalar(select(func.count()).select_from(User)) or 0
    except Exception as e:  # noqa: BLE001 表还没建时不该让启动失败
        logger.warning("[bootstrap] 读取 users 表失败（跳过引导管理员）: %s", e)
        db.rollback()
        return None, False

    if total == 0:
        email, password, display, used_fallback = _resolve_admin_credentials()
        admin = User(
            id=uuid.uuid4().hex,
            email=email.lower(),
            password_hash=hash_password(password),
            display_name=display,
            role="admin",
            is_active=True,
        )
        db.add(admin)
        db.commit()
        logger.warning(
            "[bootstrap] 已创建引导管理员 %s（role=admin）%s",
            email,
            "；⚠️ 口令用的是默认值，请登录后立即修改，或改 .env 的 BOOTSTRAP_ADMIN_PASSWORD"
            if used_fallback
            else "",
        )
        return admin, True

    # 已有用户：挑存量数据的归属者
    email = (settings.bootstrap_admin_email or "").strip().lower()
    owner = None
    if email:
        owner = db.scalar(select(User).where(User.email == email).limit(1))
    if owner is None:
        owner = db.scalar(
            select(User).where(User.role == "admin").order_by(User.created_at.asc()).limit(1)
        )
    if owner is None:
        owner = db.scalar(select(User).order_by(User.created_at.asc()).limit(1))
    return owner, False


def backfill_ownership(db: OrmSession, owner: User | None) -> dict[str, int]:
    """把无归属的存量行挂到 `owner` 名下。

    - `sessions.user_id IS NULL` → owner
    - `tool_call_logs.user_id IS NULL` → 按 `session_id` join 会话归属
    - `audit_logs.user_id IS NULL` → 按 `thread_id` join 会话归属

    日志表**不**直接挂 owner：日志的归属应该跟着它记的那个会话走，
    否则"会话已归属给 A、它的日志却归到管理员"会让人查不明白。

    Returns:
        各表的处理条数与剩余孤儿数（孤儿 = 会话已被删除，无处可挂）。
    """
    result = {"sessions": 0, "tool_call_logs": 0, "audit_logs": 0,
              "orphan_tool_call_logs": 0, "orphan_audit_logs": 0}
    if owner is None:
        logger.info("[bootstrap] 没有可用的归属账号，跳过存量回填")
        return result

    try:
        # ---- 会话 ----
        res = db.execute(
            update(DbSession).where(DbSession.user_id.is_(None)).values(user_id=owner.id)
        )
        result["sessions"] = res.rowcount or 0

        # ---- 工具流水：按会话归属（关联子查询，两种数据库都支持）----
        session_owner = (
            select(DbSession.user_id)
            .where(DbSession.id == ToolCallLog.session_id)
            .scalar_subquery()
        )
        res = db.execute(
            update(ToolCallLog)
            .where(ToolCallLog.user_id.is_(None), session_owner.is_not(None))
            .values(user_id=session_owner)
        )
        result["tool_call_logs"] = res.rowcount or 0

        # ---- 审计日志：按 thread_id 归属 ----
        thread_owner = (
            select(DbSession.user_id)
            .where(DbSession.id == AuditLog.thread_id)
            .scalar_subquery()
        )
        res = db.execute(
            update(AuditLog)
            .where(AuditLog.user_id.is_(None), thread_owner.is_not(None))
            .values(user_id=thread_owner)
        )
        result["audit_logs"] = res.rowcount or 0

        db.commit()

        result["orphan_tool_call_logs"] = db.scalar(
            select(func.count()).select_from(ToolCallLog).where(ToolCallLog.user_id.is_(None))
        ) or 0
        result["orphan_audit_logs"] = db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.user_id.is_(None))
        ) or 0
    except Exception as e:  # noqa: BLE001 回填失败不能挡住启动（新数据依然带归属）
        logger.warning("[bootstrap] 存量归属回填失败: %s", e)
        db.rollback()
        return result

    if any(result[k] for k in ("sessions", "tool_call_logs", "audit_logs")):
        logger.info(
            "[bootstrap] 存量归属已回填到 %s：会话 %d 条 / 工具流水 %d 条 / 审计 %d 条",
            owner.email, result["sessions"], result["tool_call_logs"], result["audit_logs"],
        )
    if result["orphan_tool_call_logs"] or result["orphan_audit_logs"]:
        # 这些行的会话已经被删掉了，挂不上去。它们归属 NULL ⇒ 任何用户的查询都
        # 匹配不到 ⇒ 不会被谁看到（fail-closed），留着不影响隔离，只是占空间。
        logger.info(
            "[bootstrap] 剩余无归属日志（对应会话已删除）：工具流水 %d 条 / 审计 %d 条",
            result["orphan_tool_call_logs"], result["orphan_audit_logs"],
        )
    return result


def run_bootstrap() -> None:
    """启动引导入口：引导管理员 + 存量回填 + 工作区搬迁 + 系统级供应商。"""
    from app.agent import providers as provider_svc
    from app.core.workspace import migrate_legacy_workspace
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        owner, created = ensure_bootstrap_admin(db)
        backfill_ownership(db, owner)
        if created and owner is not None:
            # 新建管理员时，存量数据本来就属于它，上面这次回填已经处理完了
            logger.info("[bootstrap] 引导完成（新建管理员 %s）", owner.email)

        # P18：把旧布局的工作区（全部文件都摊在 <base>/ 根下）搬进
        # <base>/<引导管理员 id>/。幂等 + 先备份，细节见 app/core/workspace.py。
        result = migrate_legacy_workspace(owner.id if owner else None)
        if result.get("moved"):
            logger.warning(
                "[bootstrap] 工作区已按用户隔离：搬迁 %d 项，备份在 %s",
                len(result["moved"]), result.get("backup") or "(未生成)",
            )
        elif result.get("note"):
            logger.info("[bootstrap] 工作区搬迁：%s", result["note"])

        # P18（2026-09-18 调整）：不再创建"系统级供应商"（改成每个用户必须自己配模型，
        # 见 providers 模块头注释）。但早期版本建过一条，这里把它**收编**成引导管理员的
        # 私有供应商 —— 密钥原样保留，使用方不需要重新填一遍。
        provider_svc.migrate_system_providers(db, owner.id if owner else None)
    except Exception as e:  # noqa: BLE001
        logger.warning("[bootstrap] 引导流程失败（不影响服务启动）: %s", e)
    finally:
        db.close()


__all__ = ["backfill_ownership", "ensure_bootstrap_admin", "run_bootstrap"]
