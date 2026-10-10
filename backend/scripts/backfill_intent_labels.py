"""回填历史 `tool_call` 事件的意图标签。

## 干什么

扫描 `tool_call_logs`（= `tool_call` 事件的存储）里 `intent_label IS NULL`
的历史记录，按「工具名 + 参数」补一个**确定性**标签，来源标成 `backfill`，
然后把受影响的会话投影到 `task_steps`（看板读模型）。

## 三条自我约束

1. **不调 LLM。** 存量流水有几万条，逐条过一次模型既慢又贵，而且"每次跑出来
   可能不一样"就不叫回填了。`deterministic_label()` 只看参数里最有信息量的
   那个值（`query` / `file_path` / `command` …），同输入同输出。
2. **幂等。** 只补"没有标签"的行，已经有了的一个字节都不动；投影构建器
   （`project_session`）本身是全量收敛，第二次跑 `0 inserted / 0 updated /
   0 deleted`。所以这个脚本可以放心地重复执行、也可以中途 Ctrl-C 再跑。
3. **只写标签相关的列。** 不碰 `result` / `status` / `user_id` —— 那些是运行期
   的事实，回填脚本没有资格改写它们。

## 用法

    # 预演：只统计，不写库
    python -m scripts.backfill_intent_labels --dry-run

    # 回填全部会话
    python -m scripts.backfill_intent_labels

    # 只回填一个会话（排查时用）
    python -m scripts.backfill_intent_labels --session <session_id>

    # 只看前 N 条（分批回填大库）
    python -m scripts.backfill_intent_labels --limit 5000

退出码：0 = 正常（含"没有需要回填的"）；1 = 出错了（异常信息在 stderr）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# 允许 `python scripts/backfill_intent_labels.py` 这种直接执行的姿势
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.agent.intent import SOURCE_BACKFILL, deterministic_label  # noqa: E402
from app.db.models import TaskStep, ToolCallLog  # noqa: E402,F401  确保模型全部注册
from app.db.projections import project_session  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def _parse_args(raw: str) -> dict:
    """把落库的 JSON 参数串解回来（坏数据当空 dict，不要让整批回填失败）。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}


def backfill(*, session_id: str | None = None, limit: int | None = None,
             dry_run: bool = False, verbose: bool = False) -> dict:
    """执行回填；返回计数汇总（也是测试断言的对象）。

    Returns:
        `{"scanned", "labeled", "sessions", "steps_changed", "sources"}`
        其中 `sources` 是本次写入的标签来源分布（正常只有 `backfill`）。
    """
    db = SessionLocal()
    stats: Counter = Counter()
    try:
        query = db.query(ToolCallLog)
        if session_id:
            query = query.filter(ToolCallLog.session_id == session_id)
        # 只取"没标签"的行：`IS NULL` 与空串都算（空串是更早版本可能写出的形态）
        query = query.filter(
            (ToolCallLog.intent_label.is_(None)) | (ToolCallLog.intent_label == "")
        ).order_by(ToolCallLog.id.asc())
        if limit:
            query = query.limit(int(limit))

        rows = query.all()
        stats["scanned"] = len(rows)
        touched_sessions: set[str] = set()

        for row in rows:
            label = deterministic_label(row.name or "", _parse_args(row.args or ""))
            if verbose:
                print(f"  #{row.id} {row.name} -> {label}")
            row.intent_label = label
            row.intent_source = SOURCE_BACKFILL
            stats["labeled"] += 1
            stats[f"source:{SOURCE_BACKFILL}"] += 1
            touched_sessions.add(row.session_id or "")

        # 投影收敛：把补好的标签同步进看板读模型。
        # 注意这里对**收到影响的会话**重建，而不是逐条 upsert —— 后者会在
        # "日志被删/被改"时留下孤儿 step 行（见 app/db/projections.py 模块注释）。
        for sid in sorted(s for s in touched_sessions if s):
            changed = project_session(db, sid)
            stats["sessions"] += 1
            stats["steps_changed"] += sum(changed.values())

        if dry_run:
            db.rollback()
        else:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {
        "scanned": stats["scanned"],
        "labeled": stats["labeled"],
        "sessions": stats["sessions"],
        "steps_changed": stats["steps_changed"],
        "sources": {k.split(":", 1)[1]: v for k, v in stats.items() if k.startswith("source:")},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="为历史 tool_call 事件回填 intent_label（确定性、幂等、不调 LLM）"
    )
    parser.add_argument("--session", default="", help="只处理该会话（默认全部）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（0 = 不限）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument("--verbose", action="store_true", help="逐条打印补出来的标签")
    args = parser.parse_args(argv)

    try:
        result = backfill(
            session_id=args.session or None,
            limit=args.limit or None,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
    except Exception as e:  # noqa: BLE001 脚本要把错误说清楚再退出
        print(f"[backfill] 失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    mode = "（dry-run，未写库）" if args.dry_run else ""
    print(
        f"[backfill] 扫描 {result['scanned']} 条，补标签 {result['labeled']} 条，"
        f"重建 {result['sessions']} 个会话的看板（改动 {result['steps_changed']} 行）"
        f"{mode}"
    )
    if result["labeled"] == 0:
        print("[backfill] 没有需要回填的记录（本次 0 改动 = 幂等）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
