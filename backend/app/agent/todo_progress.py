"""Incremental, model-reported work progress; not a task-success detector."""
from collections import defaultdict

from langchain.agents.middleware.todo import (
    TodoListMiddleware,
    WRITE_TODOS_SYSTEM_PROMPT,
    WRITE_TODOS_TOOL_DESCRIPTION,
)
from langchain_core.messages import ToolMessage


PROGRESS_POLICY = """
## 任务清单的含义和逐项更新协议
- write_todos 跟踪的是你为当前用户请求正在执行的工作，完成状态由你负责如实报告。
- 给用户制定学习、健身、旅行等未来计划时，清单应写“梳理知识模块”“安排学习顺序”
  “编写练习与验收标准”等本次交付步骤。未来计划的章节、用户还没学过的知识点，
  不等于你已经执行完成的任务；不能因为写好了路线文档就把所有学习阶段标为完成。
- 开始工作前保存清单；未开始的任务为 pending，正在执行的任务为 in_progress。
- 每完成一项就立刻调用 write_todos，保持其他任务的状态；可同时将下一项设为 in_progress。
  每次最多新增一个 completed；禁止最后把所有任务批量打钩，也不要为了动效空转状态。
- 只有上一次已保存为 in_progress 的同一任务，才能在后续调用中改为 completed。
  新增或改名后的任务先保存为 pending / in_progress；保持任务名称稳定以便追踪。
- 写入清单成功只说明状态保存成功，不是工作成果通过验证。完成前核对真实结果；
  失败、暂停、等待用户或没有证据时保留未完成状态，不能用结束回复代替完成验证。
- 工具拒绝更新时，原清单保持不变。按错误说明修正，不要把同一批完成状态重复提交。
""".strip()


def _indexed(items):
    occurrences = defaultdict(int)
    for item in items:
        text = item["content"].strip()
        index = occurrences[text]
        occurrences[text] += 1
        yield (text, index), item["status"]


def _valid_items(items):
    return isinstance(items, list) and all(
        isinstance(item, dict) and isinstance(item.get("content"), str)
        and item.get("status") in {"pending", "in_progress", "completed"} for item in items)


def progress_error(previous, proposed):
    """Reject unstarted/batched completions without changing persisted state.

    Schema errors belong to the tool's normal validation. This checks only the
    transition protocol; it cannot verify arbitrary natural-language outcomes.
    """
    if not _valid_items(proposed):
        return None
    prior = dict(_indexed(previous)) if _valid_items(previous) else {}
    changes = [(key, status) for key, status in _indexed(proposed)
               if status == "completed" and prior.get(key) != "completed"]
    unstarted = [key[0] for key, _ in changes if prior.get(key) != "in_progress"]
    if unstarted:
        labels = "、".join(text[:60] for text in unstarted[:3])
        return ("本次清单未保存：不能把未开始或改名后的任务直接标为 completed（" + labels
                + "）。先保存当前实际开始的一项为 in_progress，完成后再单独确认；其他项保留原状态。")
    if len(changes) > 1:
        return "本次清单未保存：每次最多新增一个 completed，请在每项实际完成时分别更新，不要批量报完成。"
    return None


class TodoProgressMiddleware(TodoListMiddleware):
    """Keep the framework's todo tool/state and enforce incremental transitions."""

    @property
    def name(self):
        # DeepAgents replaces built-in middleware by name. A different name
        # would leave two todo middlewares/tools with conflicting policies.
        return "TodoListMiddleware"

    def __init__(self):
        super().__init__(
            system_prompt=WRITE_TODOS_SYSTEM_PROMPT + "\n\n" + PROGRESS_POLICY,
            tool_description=WRITE_TODOS_TOOL_DESCRIPTION + "\n\n" + PROGRESS_POLICY,
        )

    @staticmethod
    def _rejection(request):
        if request.tool_call["name"] != "write_todos":
            return None
        error = progress_error(request.state.get("todos") or [], request.tool_call.get("args", {}).get("todos"))
        if error:
            return ToolMessage(content="Error: " + error, status="error", name="write_todos",
                               tool_call_id=request.tool_call["id"])
        return None

    def wrap_tool_call(self, request, handler):
        rejection = self._rejection(request)
        return rejection if rejection is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        rejection = self._rejection(request)
        return rejection if rejection is not None else await handler(request)
