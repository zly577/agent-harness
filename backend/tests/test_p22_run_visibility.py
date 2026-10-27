"""测试：运行可见性 —— 「到底还在不在跑 / 操作了什么 / 成没成」。

覆盖四块：

| 关注点 | 用例 |
|--------|------|
| 成败判定不误报 | `test_classify_success_output_containing_error_word`（正文里有 "error" 不许判失败）、`test_classify_failed_exit_code` |
| 成败文案 | `test_outcome_label_*` |
| 自杀式命令护栏 | `test_find_self_kill_*`、`test_guard_blocks_without_calling_handler`、`test_guard_does_not_touch_precise_pid_kill` |
| 回放也带状态与成败 | `test_merge_tool_log_brings_status_and_outcome` |

事故背景（2026-10-09）：模型执行
`Stop-Process -Name python -Force -ErrorAction SilentlyContinue` 想重启 9000 端口的
服务，结果把跑这次对话的后端也杀了 ⇒ 那一轮永远没有 `tool_result` ⇒ 前端工具卡
一直显示"进行中"。本文件守住"下次还能被拦住 + 被看清"。
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import ToolMessage

from app.agent.middleware import (
    SelfKillGuardMiddleware,
    build_self_kill_guard_middleware,
    find_self_kill_command,
)
from app.agent.runtime import _merge_tool_log
from app.agent.tool_outcome import classify_tool_result, outcome_label


# ---------------------------------------------------------------- 成败判定

def test_classify_failed_exit_code():
    out = classify_tool_result("execute", "boom\n\n[Command failed with exit code 3]")
    assert out["ok"] is False
    assert out["exit_code"] == 3
    assert out["reason"] == "退出码 3"


def test_classify_succeeded_exit_code():
    out = classify_tool_result("execute", "ok\n\n[Command succeeded with exit code 0]")
    assert out["ok"] is True
    assert out["exit_code"] == 0


def test_classify_success_output_containing_error_word():
    """🔥 反例守卫：正文里出现 error / Traceback 不代表失败。

    如果这里判成失败，用户会以为操作没成、把已经做对的事情重做一遍。
    真正的成败只写在末尾的 `[...]` 尾巴上。
    """
    text = (
        "正在扫描日志...\n"
        "2026-10-09 ERROR connection reset\n"
        "Traceback (most recent call last):\n"
        "  该行只是日志内容\n"
        "[Command succeeded with exit code 0]"
    )
    assert classify_tool_result("execute", text)["ok"] is True

    # 没有退出码尾巴、但正文里散落 error 字样 → 中性偏成功（只认 `Error:` 前缀）
    assert classify_tool_result("read_file", "log line: error 500")["ok"] is True


def test_classify_error_prefix():
    out = classify_tool_result("read_file", "Error: 文件不存在 /workspace/x")
    assert out["ok"] is False
    assert out["reason"].startswith("文件不存在")


def test_classify_empty_is_unknown():
    """空输出既不能说成功也不能说失败 —— 前端要显示中性的"已返回"。"""
    out = classify_tool_result("execute", "   \n  ")
    assert out["ok"] is None


def test_classify_last_exit_code_wins():
    """管道里可能同时出现两处尾巴，取**最后**一处。"""
    text = "[Command failed with exit code 1]\n恢复后重跑\n[Command succeeded with exit code 0]"
    assert classify_tool_result("execute", text)["ok"] is True


@pytest.mark.parametrize("name", ["read_file", "grep", "task"])
def test_quoted_exit_code_is_not_a_tool_failure(name):
    text = "题目里的工具记录：\n[Command failed with exit code 7]"
    assert classify_tool_result(name, text) == {"ok": True, "exit_code": None, "reason": ""}


def test_read_evaluation_json_with_quoted_failure():
    text = json.dumps({"tasks": [{"id": "A15", "question": (
        "[tool_record] 命令：python report.py\n输出：FileNotFoundError: input.csv\n"
        "[Command failed with exit code 7]\n独立检查：output.json不存在。"
    ), "answer": None}]}, ensure_ascii=False, indent=2)
    numbered = "\n".join(f"{i:3}  {line}" for i, line in enumerate(text.splitlines(), 1))
    assert classify_tool_result("read_file", numbered) == {"ok": True, "exit_code": None, "reason": ""}


@pytest.mark.parametrize("text", [
    "quoted: [Command failed with exit code 7]",
    "[Command failed with exit code 7]\n后续正文",
])
def test_execute_requires_a_terminal_status_line(text):
    assert classify_tool_result("execute", text) == {"ok": True, "exit_code": None, "reason": ""}


def test_execute_failure_with_trailing_whitespace():
    out = classify_tool_result("execute", "boom\r\n[Command failed with exit code 7]\r\n  ")
    assert out == {"ok": False, "exit_code": 7, "reason": "退出码 7"}


def test_outcome_label_formats():
    assert outcome_label({"ok": True, "reason": ""}, 3200) == "成功 · 3.2s"
    assert outcome_label({"ok": False, "reason": "退出码 1"}, 800) == "失败（退出码 1） · 0.8s"
    assert outcome_label({"ok": None, "reason": "（无输出）"}, None) == "已返回"
    assert outcome_label(None, 125000) == "已返回 · 2m5s"


# ---------------------------------------------------------------- 护栏

#: 事故里真实出现过的那条命令（原样抄下来，防回归）
ACCIDENT_COMMAND = (
    'Stop-Process -Name python -Force -ErrorAction SilentlyContinue; '
    'Start-Sleep -Seconds 1; '
    'Start-Process python "C:\\Users\\Administrator\\Desktop\\auth_app.py" "9000" '
    '-WindowStyle Hidden; Start-Sleep -Seconds 2; netstat -ano | findstr "9000"'
)


@pytest.mark.parametrize("command", [
    ACCIDENT_COMMAND,
    "stop-process -name python -force",
    'Stop-Process -Name "pythonw" -Force',
    "taskkill /F /IM python.exe",
    "pkill -f python",
    "killall python3",
    "wmic process where \"name='python.exe'\" delete",
    "Get-Process -Name python | Stop-Process -Force",
])
def test_find_self_kill_hits(command):
    assert find_self_kill_command({"command": command}), command


@pytest.mark.parametrize("command", [
    # 正解：按端口定位、按 PID 精确结束 —— 这是我们在提示词里推荐用户用的方式
    'netstat -ano | findstr ":9000"',
    "Stop-Process -Id 12345 -Force",
    "python auth_app.py 9000",
    "taskkill /PID 4321 /F",
    "dir D:\\proj",
    "",
])
def test_find_self_kill_no_false_positive(command):
    assert find_self_kill_command({"command": command}) is None, command


def test_find_self_kill_scans_nested_args():
    """命令可能藏在嵌套结构里（MCP 工具的 schema 千奇百怪）。"""
    args = {"steps": [{"cmd": "taskkill /F /IM python.exe"}], "meta": {"note": "x"}}
    assert find_self_kill_command(args) is not None


class _FakeRequest:
    def __init__(self, name, args, call_id="call_1"):
        self.tool_call = {"name": name, "args": args, "id": call_id}


def test_guard_blocks_without_calling_handler():
    """命中就短路：handler 一次都不许被调用（调用即等于执行）。"""
    called = {"n": 0}

    async def handler(_request):
        called["n"] += 1
        return ToolMessage(content="ran", tool_call_id="call_1")

    mw = build_self_kill_guard_middleware()
    import asyncio

    result = asyncio.run(mw.awrap_tool_call(
        _FakeRequest("PowerShell", {"command": ACCIDENT_COMMAND}), handler))

    assert called["n"] == 0
    assert isinstance(result, ToolMessage)
    # 内容必须以 `Error:` 开头 —— 这样 classify_tool_result 会判失败，卡片显示"失败"
    assert result.content.startswith("Error:")
    assert classify_tool_result("PowerShell", result.content)["ok"] is False
    # status 留 success ⇒ 不把这次拦截记进熔断器
    assert result.status == "success"


def test_guard_passes_normal_command_through():
    seen = {}

    async def handler(request):
        seen["args"] = request.tool_call["args"]
        return ToolMessage(content="ok", tool_call_id="call_2")

    import asyncio

    result = asyncio.run(SelfKillGuardMiddleware().awrap_tool_call(
        _FakeRequest("execute", {"command": 'netstat -ano | findstr ":9000"'}, "call_2"),
        handler,
    ))
    assert seen["args"]["command"].startswith("netstat")
    assert result.content == "ok"


def test_guard_middleware_identity():
    assert build_self_kill_guard_middleware().name == "harness-self-kill-guard"


def test_subagent_also_carries_the_guard():
    """子智能体不继承主 Agent 的中间件栈 —— 必须单独挂，否则照样能杀掉后端。"""
    from app.agent.subagents import SubAgentConfig, build_subagent

    sub = build_subagent(SubAgentConfig(name="probe", description="d", system_prompt="p"))
    mw_names = [getattr(m, "name", "") for m in (sub.get("middleware") or [])]
    assert "harness-self-kill-guard" in mw_names
    assert "harness-intent" in mw_names or any("intent" in n for n in mw_names)


# ---------------------------------------------------------------- 回放


@pytest.mark.parametrize("rebuild", [False, True])
def test_replay_corrects_quoted_failure_badge(rebuild):
    history = [{"role": "assistant", "content": "", "toolCalls": [] if rebuild else [
        {"id": "c1", "name": "read_file", "result": "", "status": "error"},
    ]}]
    log_rows = [{"id": "c1", "name": "read_file", "kind": "tool", "status": "error",
                 "args": {}, "result": '1  {"record": "[Command failed with exit code 7]"}'}]
    tc = _merge_tool_log(history, log_rows)[-1]["toolCalls"][0]
    assert tc["status"] == "done"
    assert tc["ok"] is True
    assert tc["exitCode"] is None

def test_merge_tool_log_brings_status_and_outcome():
    """被强杀的工具，刷新页面后不能"变成已完成"。"""
    history = [{
        "role": "assistant",
        "content": "",
        "toolCalls": [
            {"id": "c1", "name": "PowerShell", "args": {}, "result": "", "status": "done"},
            {"id": "c2", "name": "execute", "args": {}, "result": "", "status": "done"},
        ],
        "delegations": [],
    }]
    log_rows = [
        {"id": "c1", "name": "PowerShell", "kind": "tool", "status": "interrupted",
         "args": {}, "result": "（上一轮运行被中断，未返回结果）",
         "intent_label": None, "intent_source": None},
        {"id": "c2", "name": "execute", "kind": "tool", "status": "error",
         "args": {}, "result": "boom\n\n[Command failed with exit code 2]",
         "intent_label": None, "intent_source": None},
    ]
    out = _merge_tool_log(history, log_rows)
    first = out[0]["toolCalls"][0]
    second = out[0]["toolCalls"][1]

    assert first["status"] == "interrupted"
    assert first["ok"] is None
    assert second["status"] == "error"
    assert second["ok"] is False
    assert second["exitCode"] == 2


def test_merge_tool_log_marks_error_from_result_even_when_row_says_done():
    """🔥 老数据兜底： `status` 一律写 `done`，但结果里明明有 `Error:`。

    只看 `status` 会让这类记录显示"已完成"，而卡片上同时贴着"↳ Error: ..." ——
    自相矛盾的卡片比不显示更让人困惑。判定结果说了算。
    """
    history = [{
        "role": "assistant", "content": "",
        "toolCalls": [{"id": "c1", "name": "delete", "args": {}, "result": "", "status": "done"}],
        "delegations": [],
    }]
    log_rows = [{
        "id": "c1", "name": "delete", "kind": "tool", "status": "done",
        "args": {}, "result": "Error: '/err.log' not found",
        "intent_label": None, "intent_source": None,
    }]
    out = _merge_tool_log(history, log_rows)
    tc = out[0]["toolCalls"][0]
    assert tc["ok"] is False
    assert tc["status"] == "error"
