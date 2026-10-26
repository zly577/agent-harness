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
from typing import Any, AsyncIterator, Callable, Literal

from app.agent.events import to_sse_event

logger = logging.getLogger(__name__)

#: 一轮结束（done/error/stopped）后，事件缓冲保留多久供回放。
#: 太短：用户刷新慢一点就看不到刚结束的回答；太长：内存里长期挂着大数组。
KEEP_SECONDS = 180
SUBSCRIBER_QUEUE_SIZE = 1024
RunStatus = Literal["running", "stopping", "done", "error", "stopped", "awaiting_approval", "awaiting_input"]


class RunConflict(RuntimeError):
    """A session already has an active or suspended run."""


class RunStopped(RuntimeError):
    """A caller waiting for a reply had its run explicitly stopped."""

#: 缓冲里可以被合并的增量事件类型（value 是要拼接的字段）
_DELTA_FIELDS = {
    "token": "content",
    "thinking": "delta",
    "subagent_token": "delta",
    "subagent_thinking": "delta",
}


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
    status: RunStatus = "running"
    error: str = ""
    #: 已发生的事件（**已合并**的增量），供回放
    events: list[dict] = field(default_factory=list)
    #: 订阅者队列；SSE 连接从这里取事件。None 是"结束"哨兵
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exception: Exception | None = field(default=None, repr=False)
    expiry_handle: asyncio.TimerHandle | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.status in {"running", "stopping"}

    @property
    def suspended(self) -> bool:
        """挂在人工审批上：不算在跑，但这一轮也没结束。"""
        return self.status in {"awaiting_approval", "awaiting_input"}

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
        # start() performs its check and reservation without an await, so they
        # are atomic on the application's single event loop.

    # ---------- 查询 ----------

    def current(self, session_id: str) -> SessionRun | None:
        """当前或最近一次运行（可能已结束但还在保留期内）。"""
        run = self._runs.get(session_id)
        if run and not run.suspended and run.finished_at and (time.time() - run.finished_at) > KEEP_SECONDS:
            # 保留期已过：清掉，避免内存里长期挂着几万条事件
            self._runs.pop(session_id, None)
            return None
        return run

    def is_running(self, session_id: str) -> bool:
        run = self._runs.get(session_id)
        return bool(run and run.running)

    # ---------- 启动 / 停止 ----------

    def start(self, session_id: str, agent_input, user_message: str = "",
              user_id: str | None = None, *,
              event_stream: Callable[[], AsyncIterator[tuple[str, Any]]] | None = None) -> SessionRun:
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
            raise RunConflict("该会话已有一轮回答正在进行中")
        if existing and existing.suspended:
            from langgraph.types import Command
            if not isinstance(agent_input, Command):
                raise RunConflict("请先回答待澄清的问题或处理待审批操作")

        if existing and existing.expiry_handle:
            existing.expiry_handle.cancel()

        run = SessionRun(session_id=session_id, user_message=user_message, user_id=user_id)
        self._runs[session_id] = run
        run.task = asyncio.create_task(self._drive(run, agent_input, _runtime, event_stream))
        run.task.add_done_callback(lambda task: self._task_done(run, task))
        logger.info("[runner] 会话 %s 开始新一轮（后台运行，与连接解耦）", session_id)
        return run

    async def wait_result(self, run: SessionRun) -> str:
        """Wait for the same background run used by SSE, without cancelling it."""
        if run.task:
            try:
                await asyncio.shield(run.task)
            except asyncio.CancelledError:
                if run.status != "stopped":
                    raise
        if run.status == "stopped":
            raise RunStopped("已按你的要求停止本轮回答")
        if run.exception is not None:
            raise run.exception
        for event in reversed(run.events):
            if event["type"] == "result":
                return event.get("content") or ""
            if event["type"] == "clarification":
                return "等待你补充信息：" + "；".join(item["question"] for item in event.get("clarifications", []))
            if event["type"] == "approval":
                return "任务已暂停，等待操作审批。"
        return ""

    def _expire(self, run: SessionRun) -> None:
        run.expiry_handle = None
        if self._runs.get(run.session_id) is run and not run.running and not run.suspended:
            self._runs.pop(run.session_id, None)

    def _finish(self, run: SessionRun) -> None:
        run.finished_at = time.time()
        self._close(run)
        if not run.suspended:
            run.expiry_handle = asyncio.get_running_loop().call_later(KEEP_SECONDS, self._expire, run)

    def _task_done(self, run: SessionRun, task: asyncio.Task) -> None:
        # A task cancelled before its first step never enters _drive's finally.
        if task.cancelled() and run.finished_at is None:
            run.status = "stopped"
            self._publish(run, {"type": "stopped", "detail": "已按你的要求停止本轮回答"})
            self._finish(run)

    async def aclose(self, timeout: float = 10.0) -> bool:
        """Stop producers before application storage is closed."""
        runs = list(self._runs.values())
        tasks = [run.task for run in runs if run.task and not run.task.done()]
        for run in runs:
            if run.task in tasks:
                self.stop(run.session_id)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                logger.error("[shutdown] %d task(s) still stopping; storage must remain open", len(pending))
                return False
        for run in runs:
            if run.expiry_handle:
                run.expiry_handle.cancel()
        self._runs.clear()
        return True

    def stop(self, session_id: str) -> bool:
        """用户明确停止：取消运行 task。返回是否真的有东西被停。"""
        run = self._runs.get(session_id)
        if not run or not run.running:
            return False
        if run.status == "stopping":
            return True  # Do not cancel the task again while it is cleaning up.
        if run.task and not run.task.done():
            run.status = "stopping"
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
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            if not run.task.done():
                raise
        except Exception:  # noqa: BLE001
            pass
        return run.task.done()

    # ---------- 驱动 ----------

    async def _drive(self, run: SessionRun, agent_input, runtime_mod, event_stream=None) -> None:
        """后台跑完一轮：事件广播给订阅者、缓冲供回放。"""
        try:
            events = event_stream() if event_stream else runtime_mod.agent_event_stream(
                run.session_id, agent_input, user_id=run.user_id)
            async for kind, data in events:
                self._publish(run, to_sse_event(kind, data))
        except asyncio.CancelledError:
            # 用户点了停止 / 进程在收尾
            run.status = "stopped"
            self._publish(run, {"type": "stopped", "detail": "已按你的要求停止本轮回答"})
            raise
        except Exception as e:  # noqa: BLE001
            run.status = "error"
            run.exception = e
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
            run.status = {"approval": "awaiting_approval", "clarification": "awaiting_input"}.get(last_type, "done")
        finally:
            self._finish(run)

    def _publish(self, run: SessionRun, evt: dict) -> None:
        """广播给订阅者（细粒度）并把事件并入回放缓冲（相邻增量已合并）。"""
        for q in list(run.subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # Slow clients can reconnect and replay; never block the agent
                # or retain an unbounded per-connection copy of its output.
                self._disconnect_slow_subscriber(q)
                run.subscribers.discard(q)
            except Exception:  # noqa: BLE001 订阅者自己出问题，不能连累运行
                run.subscribers.discard(q)
        self._buffer(run, evt)

    @staticmethod
    def _disconnect_slow_subscriber(queue: asyncio.Queue) -> None:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait({"type": "error", "detail": "连接接收过慢，请刷新页面以恢复运行进度"})
        queue.put_nowait(None)

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
            except asyncio.QueueFull:
                self._disconnect_slow_subscriber(q)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 订阅（回放 + 直播） ----------

    async def subscribe(self, session_id: str, since: int | None = None, *,
                        run: SessionRun | None = None) -> AsyncIterator[dict]:
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
        run = run if run is not None else self.current(session_id)
        if run is None:
            return
        if run.session_id != session_id:
            raise ValueError("订阅的运行与会话不匹配")

        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        # 先订阅、再回放：否则回放期间产生的新事件会丢
        run.subscribers.add(queue)
        try:
            # 回放的是快照副本，避免迭代过程中缓冲被 _buffer 修改
            events = [dict(event) for event in run.events]
            start = 0 if since is None else max(0, min(int(since), len(events)))
            for evt in events[start:]:
                yield evt
            # A run may finish while the replay is being sent. Its final live
            # events and end sentinel are already queued and must be drained.
            if not run.running and queue.empty():
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


__all__ = ["KEEP_SECONDS", "RunConflict", "RunStopped", "RunManager", "SessionRun", "run_manager", "to_sse_event"]
