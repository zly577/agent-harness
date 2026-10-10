"""把 agent 运行从 HTTP 请求的生命周期里剥离出来。

## 问题：SSE 连接**就是**运行本身

改造前 `POST /chat/stream` 的生成器直接 `async for chunk in runtime.stream_chat(...)`：

    StreamingResponse(_gen())   # _gen 里就是驱动 agent 的那个协程

FastAPI/Starlette 在客户端断开时会**取消**这个任务，于是 `agent.astream(...)`
被取消 —— 这一轮回答直接蒸发。表现就是用户抱怨的：

- 刷新页面 → 回答没了
- 关掉标签页 → 回答没了
- 网络抖一下就断 → 回答没了
- 只有"页面一直开着不动"才能跑完，这显然不合理

## 解法：运行归运行，连接归连接

运行交给一个**不属于任何请求**的后台 task；事件同时做两件事：

1. **广播**给当前所有订阅者（SSE 连接只是订阅者之一）；
2. **缓冲**成一份可回放的序列。

于是 HTTP 连接断了不影响运行；重连（刷新 / 切回来）时先回放缓冲、再接上直播，
用户看到的就是"它一直在跑，我只是换了个窗口看"。

只有两种情况会真的停：
- `POST /sessions/{id}/stop`（用户明确点停止）
- 进程退出

## 缓冲为什么要"合并"

一轮调研实测能产生 1.6 万条事件，其中绝大多数是逐 token 的增量。
原样堆在内存里既浪费又没必要 —— 于是在**写入缓冲时**把相邻的同类增量合并成一条
（`token` 接 `token`、`subagent_thinking` 接 `subagent_thinking` …）。
注意：合并只发生在缓冲里；**订阅者拿到的仍是细粒度事件**，所以在线用户的
逐字流式体验完全不受影响。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

#: 一轮结束（done/error/stopped）后，事件缓冲保留多久供回放。
#: 太短：用户刷新慢一点就看不到刚结束的回答；太长：内存里长期挂着大数组。
KEEP_SECONDS = 180

#: 缓冲里可以被合并的增量事件类型（value 是要拼接的字段）
_DELTA_FIELDS = {
    "token": "content",
    "thinking": "delta",
    "subagent_token": "delta",
    "subagent_thinking": "delta",
}


def to_sse_event(kind: str, data: Any) -> dict:
    """runtime 事件 (kind, data) → 前端事件字典（前后端契约就在这里）。

    从 `api/routes/chat_stream.py` 搬过来：事件形状是 **agent 侧**的契约，
    路由层只负责把它序列化成 `data: {...}\n\n`。
    """
    if kind == "token":
        return {"type": "token", "content": data}
    if kind == "thinking":
        return {"type": "thinking", **data}
    if kind == "tool_call":
        return {"type": "tool_call", **data}
    if kind == "tool_result":
        return {"type": "tool_result", **data}
    # 工具参数**还在生成**时的节流心跳（大参数要几十秒到几分钟）。
    # 它存在理由和 `thinking` 一样：那段时间本来一个事件都没有，用户无法区分
    # "在写参数"和"挂了"。**不含 args 正文**，只有体积/耗时，所以可以直接推给前端。
    if kind == "tool_args":
        return {"type": "tool_args", **data}
    # 子智能体过程（把 task 委派的内部进展暴露给前端）
    if kind == "subagent_start":
        return {"type": "subagent_start", **data}
    if kind == "subagent_token":
        return {"type": "subagent_token", **data}
    if kind == "subagent_thinking":
        return {"type": "subagent_thinking", **data}
    if kind == "subagent_tool":
        return {"type": "subagent_tool", **data}
    if kind == "subagent_end":
        return {"type": "subagent_end", **data}
    if kind == "subagent_usage":
        return {"type": "subagent_usage", **data}
    if kind == "usage":
        return {"type": "usage", **data}
    if kind == "approval":
        return {
            "type": "approval",
            "pending": bool(data.get("pending")),
            "actions": data.get("actions") or [],
            # 停在审批上时，模型可能已经说过一段话了：把它带出去让前端落进气泡，
            # 否则用户看到的只有"弹窗"+"空气泡"（观感：工具都跑完了却什么都没说）。
            "text": data.get("text") or "",
        }
    if kind == "result":
        return {"type": "result", "content": data}
    if kind == "error":
        return {"type": "error", "detail": data}
    return {"type": kind, "content": data}


@dataclass
class SessionRun:
    """某个会话当前（或最近一次）的运行。"""

    session_id: str
    #: 触发本轮的用户消息（前端据此判断历史里哪一条属于本轮，避免重复渲染）
    user_message: str = ""
    #: 本轮归属的用户。**显式在 start() 时捕获**，不从请求上下文里读 ——
    #: 这个对象活在后台 task 里，早于/晚于请求都不成立，读"当前用户"必然出错。
    #: 它的用途是给工作区/长期记忆定位，以及给审计留痕；
    #: **授权判定仍然以「sessions.user_id」为唯一事实来源**（见 subscribe 的说明）。
    user_id: str | None = None
    #: running | done | error | stopped
    status: str = "running"
    error: str = ""
    #: 已发生的事件（**已合并**的增量），供回放
    events: list[dict] = field(default_factory=list)
    #: 订阅者队列；SSE 连接从这里取事件。None 是"结束"哨兵
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def suspended(self) -> bool:
        """挂在人工审批上：不算在跑，但这一轮也没结束。"""
        return self.status == "awaiting_approval"

    def snapshot(self) -> dict:
        """给前端的运行状态（不含事件体，事件走 SSE 回放）。"""
        return {
            "session_id": self.session_id,
            "running": self.running,
            "suspended": self.suspended,
            "status": self.status,
            "error": self.error,
            "user_message": self.user_message,
            "event_count": len(self.events),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class RunManager:
    """会话 → 运行。进程内单例（`run_manager`）。"""

    def __init__(self) -> None:
        self._runs: dict[str, SessionRun] = {}
        #: 运行启动/停止的互斥；subscribe 不参与（它只读快照 + 收队列）
        self._lock = asyncio.Lock()

    # ---------- 查询 ----------

    def current(self, session_id: str) -> SessionRun | None:
        """当前或最近一次运行（可能已结束但还在保留期内）。"""
        run = self._runs.get(session_id)
        if run and run.finished_at and (time.time() - run.finished_at) > KEEP_SECONDS:
            # 保留期已过：清掉，避免内存里长期挂着几万条事件
            self._runs.pop(session_id, None)
            return None
        return run

    def is_running(self, session_id: str) -> bool:
        run = self._runs.get(session_id)
        return bool(run and run.running)

    # ---------- 启动 / 停止 ----------

    def start(self, session_id: str, agent_input, user_message: str = "",
              user_id: str | None = None) -> SessionRun:
        """起一轮运行（后台 task，不属于任何请求）。

        Args:
            agent_input: `{"messages": [...]}` 或 `Command(resume=...)`。
            user_message: 本轮的用户消息原文（回放/去重用）。
            user_id: 本轮归属用户。在**请求时刻**由调用方捕获传进来 ——
                后台 task 里没有"当前用户"这个东西，等到执行时再查就晚了。

        Raises:
            RuntimeError: 该会话已有运行在跑 —— 并发改同一份 checkpoint 会互相破坏，
                所以这里直接拒绝，调用方应把它翻译成 409。
        """
        from app.agent import runtime as _runtime

        existing = self.current(session_id)
        if existing and existing.running:
            raise RuntimeError("该会话已有一轮回答正在进行中")

        run = SessionRun(session_id=session_id, user_message=user_message, user_id=user_id)
        self._runs[session_id] = run
        run.task = asyncio.ensure_future(self._drive(run, agent_input, _runtime))
        logger.info("[runner] 会话 %s 开始新一轮（后台运行，与连接解耦）", session_id)
        return run

    def stop(self, session_id: str) -> bool:
        """用户明确停止：取消运行 task。返回是否真的有东西被停。"""
        run = self._runs.get(session_id)
        if not run or not run.running:
            return False
        if run.task and not run.task.done():
            run.task.cancel()
            logger.info("[runner] 会话 %s 的运行已按用户要求停止", session_id)
            return True
        return False

    async def stop_and_wait(self, session_id: str, timeout: float = 5.0) -> bool:
        """停止并等它真正收尾（测试与接口用，避免"点了停止但还在跑"）。"""
        run = self._runs.get(session_id)
        if not run or not run.task:
            return False
        if not self.stop(session_id):
            return False
        try:
            await asyncio.wait_for(asyncio.shield(run.task), timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---------- 驱动 ----------

    async def _drive(self, run: SessionRun, agent_input, runtime_mod) -> None:
        """后台跑完一轮：事件广播给订阅者、缓冲供回放。"""
        try:
            async for kind, data in runtime_mod.agent_event_stream(
                run.session_id, agent_input, user_id=run.user_id
            ):
                self._publish(run, to_sse_event(kind, data))
        except asyncio.CancelledError:
            # 用户点了停止 / 进程在收尾
            run.status = "stopped"
            self._publish(run, {"type": "stopped", "detail": "已按你的要求停止本轮回答"})
            raise
        except Exception as e:  # noqa: BLE001
            run.status = "error"
            # ⚠️ 这个字符串会作为 error 事件推给前端（并留在可回放缓冲里），
            # 所以必须过 scrub_secrets：模型/SDK 的报错可能带 api_key。
            from app.agent.providers import ProviderNotConfigured
            from app.core.crypto import scrub_secrets

            # 我们自己抛的领域错误（如"还没配模型"）消息本身就是给用户看的中文，
            # 不再加 "ProviderNotConfigured: " 前缀 —— 那串类名只会让人以为系统坏了。
            if isinstance(e, ProviderNotConfigured):
                run.error = scrub_secrets(str(e))
            else:
                run.error = scrub_secrets(f"{type(e).__name__}: {e}")
            logger.exception("[runner] 会话 %s 运行失败", run.session_id)
            self._publish(run, {"type": "error", "detail": run.error})
        else:
            # 注意：`agent_event_stream` 在**等到人工审批**时也会正常返回
            # （它推一条 approval 事件然后收尾）。那种情况不能算"跑完了" ——
            # 语义上这一轮还悬着：等用户点同意/拒绝。
            # 不再分别的话，前端会以为整轮结束，刷新后既接不回来、也看不到审批弹窗。
            last_type = run.events[-1].get("type") if run.events else ""
            run.status = "awaiting_approval" if last_type == "approval" else "done"
        finally:
            run.finished_at = time.time()
            self._close(run)

    def _publish(self, run: SessionRun, evt: dict) -> None:
        """广播给订阅者（细粒度）并把事件并入回放缓冲（相邻增量已合并）。"""
        for q in list(run.subscribers):
            try:
                q.put_nowait(evt)
            except Exception:  # noqa: BLE001 订阅者自己出问题，不能连累运行
                run.subscribers.discard(q)
        self._buffer(run, evt)

    @staticmethod
    def _buffer(run: SessionRun, evt: dict) -> None:
        """写入回放缓冲：相邻的同类增量合并成一条，避免堆几万条小事件。"""
        # 进度心跳是"最新有效"语义，不是增量：同一个工具只留最后一条即可。
        # 若走下面的拼接逻辑会变成 "4032 60 88"（数字被当字符串连起来，毫无意义），
        # 而一轮 8 分钟的生成会往缓冲里堆几百条 —— 直接替换最省事也最准确。
        if str(evt.get("type")) == "tool_args":
            last = run.events[-1] if run.events else None
            if (last and last.get("type") == "tool_args"
                    and last.get("id") == evt.get("id")):
                run.events[-1] = dict(evt)
                return
            run.events.append(dict(evt))
            return
        field_name = _DELTA_FIELDS.get(str(evt.get("type")))
        if field_name and run.events:
            last = run.events[-1]
            if (last.get("type") == evt.get("type")
                    and last.get("scope") == evt.get("scope")
                    and last.get("id") == evt.get("id")):
                last[field_name] = str(last.get(field_name, "")) + str(evt.get(field_name, ""))
                return
        run.events.append(dict(evt))

    def _close(self, run: SessionRun) -> None:
        """运行结束：给每个订阅者放一个哨兵，让它们自然收尾。"""
        for q in list(run.subscribers):
            try:
                q.put_nowait(None)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 订阅（回放 + 直播） ----------

    async def subscribe(self, session_id: str, since: int | None = None) -> AsyncIterator[dict]:
        """订阅某会话的当前运行：先回放已有事件，再接收后续直播。

        Args:
            since: 回放起点（事件下标）。
                - `None`：从缓冲开头回放 —— 刷新/重连要这个（"从头看这一轮"）。
                - 一个下标：只回放该下标之后的事件 —— 配合 `start()` 使用，表达
                  "这一轮我只关心刚起的这段增量，别把之前几千条重播一遍"。
                  审批恢复就用它：那一轮的缓冲里已经堆着审批前的全部事件
                  （实测 4636 条），全量回放会把整个回合重复渲染一次。

        用法（SSE 层）：
            async for evt in run_manager.subscribe(sid):
                yield sse(evt)
        运行已结束（且在保留期内）时：回放完就断，调用方据此收尾。
        """
        run = self.current(session_id)
        if run is None:
            return

        queue: asyncio.Queue = asyncio.Queue()
        # 先订阅、再回放：否则回放期间产生的新事件会丢
        run.subscribers.add(queue)
        try:
            # 回放的是快照副本，避免迭代过程中缓冲被 _buffer 修改
            events = list(run.events)
            start = 0 if since is None else max(0, min(int(since), len(events)))
            for evt in events[start:]:
                yield evt
            if not run.running:
                return
            while True:
                item = await queue.get()
                if item is None:  # 运行结束哨兵
                    return
                yield item
        finally:
            run.subscribers.discard(queue)


#: 进程内单例
run_manager = RunManager()


__all__ = ["KEEP_SECONDS", "RunManager", "SessionRun", "run_manager", "to_sse_event"]
