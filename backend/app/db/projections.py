"""投影构建器（P20）：`tool_call` 事件 → 任务看板的 `task_steps` 读模型。

## 为什么单独一层"投影"

`tool_call_logs` 是**事件存储**：它要如实记录发生过的每一次工具调用（含被中断的、
含已被上下文压缩裁剪的、含结果永远没回来的）。而任务看板只需要回答
"这一轮走到哪一步了、那一步在干什么"。两者的字段、生命周期、清理策略都不一样，
所以中间放一层投影：

    tool_call 事件 ──(写入时同步投影)──▶ task_steps ──▶ 看板 / 回放

## 幂等（这是本模块的硬约束）

`project_session()` 是**全量收敛**：给定库里现有的 `tool_call_logs`，
它把 `task_steps` 调整到与之逐字段一致的状态，然后返回改了多少行。
所以：

- 同一份事件存储连续构建两次 → 第二次 `inserted=0, updated=0, deleted=0`
  （回填脚本"连跑两次第二次不产生改动"的验收就靠这条）；
- `tool_call_logs` 里没有的 `call_id` → 对应 step 行被删掉（不会留孤儿）。

对比 `upsert` 式的"逐条写"：那种写法在"事件被删/被回填修正"时会留下不一致的
残留，而这个投影只有一条真理来源。
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from sqlalchemy.orm import Session as OrmSession

from app.agent.intent import catalog_entry
from app.db.models import TaskStep, ToolCallLog

logger = logging.getLogger(__name__)

#: 会被搬进读模型的字段（顺序即对比顺序）
_PROJECTED_FIELDS = (
    "step_index", "tool_name", "display_name", "kind", "status",
    "intent_label", "intent_source", "user_id",
)


def _desired_rows(db: OrmSession, session_id: str) -> list[dict[str, Any]]:
    """从事件存储算出该会话"应该长什么样"的 step 列表（按发生顺序）。"""
    logs = (db.query(ToolCallLog)
            .filter(ToolCallLog.session_id == session_id)
            .order_by(ToolCallLog.id.asc()).all())
    rows: list[dict[str, Any]] = []
    for idx, log in enumerate(logs, start=1):
        name = log.name or ""
        rows.append({
            "session_id": session_id,
            "call_id": log.call_id or "",
            "user_id": log.user_id,
            "step_index": idx,
            "tool_name": name,
            "display_name": catalog_entry(name)["display_name"] or name,
            "kind": log.kind or "tool",
            "status": log.status or "running",
            "intent_label": log.intent_label or None,
            "intent_source": log.intent_source or None,
        })
    return rows


def project_session(db: OrmSession, session_id: str) -> dict[str, int]:
    """把某会话的 `task_steps` 收敛到与事件存储一致；返回改动计数。

    调用方负责 `db.commit()`（写路径上它通常和事件写入是同一事务的语义单元）。
    `call_id` 为空的行**不参与投影** —— 拿不到幂等键就没法保证重复执行不产生
    重复行，宁可不进看板（`_log_tool_event` 保证 call 事件一定带 call_id）。
    """
    desired = {r["call_id"]: r for r in _desired_rows(db, session_id) if r["call_id"]}
    existing = {s.call_id: s for s in db.query(TaskStep)
                .filter(TaskStep.session_id == session_id).all()}

    inserted = updated = deleted = 0
    for call_id, row in desired.items():
        step = existing.get(call_id)
        if step is None:
            db.add(TaskStep(**row))
            inserted += 1
            continue
        changed = False
        for field in _PROJECTED_FIELDS:
            if getattr(step, field) != row[field]:
                setattr(step, field, row[field])
                changed = True
        if changed:
            updated += 1
    for call_id, step in existing.items():
        if call_id not in desired:
            db.delete(step)
            deleted += 1

    # ⚠️ 必须先 flush 再判断"有没有变化"：新增的行此刻还在 session 里，
    #    不 flush 的话下面的 commit 才落库，调用方拿不到准确的计数。
    db.flush()
    return {"inserted": inserted, "updated": updated, "deleted": deleted}


def project_tool_call(
    db: OrmSession,
    *,
    session_id: str,
    user_id: str | None = None,
    call_id: str,
    tool_name: str,
    kind: str = "tool",
    status: str = "running",
    intent_label: str | None = None,
    intent_source: str | None = None,
) -> None:
    """写路径上的单条投影（`tool_call` 事件落库时同步调用）。

    语义上等价于"更新事件存储后重建该会话的投影"，但只碰一行 —— 工具调用是
    热路径，不能每次都扫全表。**`project_session` 仍然是权威**：两者对同一份
    数据算出的行必须完全一致（单测 `test_projection_matches_rebuild` 钉住这点）。
    """
    if not call_id:
        return
    step = (db.query(TaskStep)
            .filter(TaskStep.session_id == session_id, TaskStep.call_id == call_id)
            .first())
    if step is None:
        # 新行插在末尾：step_index 用当前最大值 +1（与 project_session 的
        # "按日志 id 顺序编号"等价，因为写入顺序 = 日志插入顺序）
        last = (db.query(TaskStep.step_index)
                .filter(TaskStep.session_id == session_id)
                .order_by(TaskStep.step_index.desc()).first())
        step = TaskStep(
            session_id=session_id,
            call_id=call_id,
            step_index=int(last[0]) + 1 if last and last[0] else 1,
            user_id=user_id,
        )
        db.add(step)

    step.tool_name = tool_name or ""
    step.display_name = catalog_entry(tool_name or "")["display_name"] or (tool_name or "")
    step.kind = kind or "tool"
    step.status = status or "running"
    if user_id:
        step.user_id = user_id
    # 标签只增不减：`result` 事件回填时不会带标签（它没有模型输出），
    # 直接覆盖会把已经写好的标签抹成 NULL。
    if intent_label is not None:
        step.intent_label = intent_label
        step.intent_source = intent_source
    db.flush()


def serialize_step(step: TaskStep) -> dict[str, Any]:
    """给 API 的序列化（字段名与 `tool_call` 事件保持一致）。"""
    return {
        "id": step.id,
        "session_id": step.session_id,
        "call_id": step.call_id,
        "step_index": step.step_index,
        "tool_name": step.tool_name,
        "display_name": step.display_name,
        "kind": step.kind,
        "status": step.status,
        "intent_label": step.intent_label,
        "intent_source": step.intent_source,
        "updated_at": step.updated_at.isoformat() if step.updated_at else None,
    }


def list_steps(db: OrmSession, session_id: str) -> list[dict[str, Any]]:
    """读某会话的步骤（按顺序）—— 任务看板的数据源。"""
    rows = (db.query(TaskStep)
            .filter(TaskStep.session_id == session_id)
            .order_by(TaskStep.step_index.asc(), TaskStep.id.asc()).all())
    return [serialize_step(s) for s in rows]


def mapping_of_steps(db: OrmSession, session_id: str) -> Mapping[str, dict[str, Any]]:
    """`call_id -> step`（回放合并用；避免调用方自己再写一遍查询）。"""
    rows = (db.query(TaskStep)
            .filter(TaskStep.session_id == session_id).all())
    return {s.call_id: serialize_step(s) for s in rows if s.call_id}


__all__ = [
    "list_steps",
    "mapping_of_steps",
    "project_session",
    "project_tool_call",
    "serialize_step",
]
