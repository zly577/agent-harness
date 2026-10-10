"""把「工具返回了什么」翻译成「这次操作成没成」（P22 运行可见性）。

## 为什么要有这个纯函数

前端工具卡上必须直接写清「成功 / 失败（退出码 N）」—— 把几 KB 的 stdout 丢给
用户自己看，等于没做这个功能。而同一份内容在**实时事件**与**历史回放**两条
路径上会被各判一次，所以判定规则只能是 `(name, content)` 的纯函数：
不许读时间、随机数、数据库或模型（重放确定性，同 P20 的 `intent.py`）。

## 🔥 判定顺序是踩过的坑

`execute` / 本机 shell 的成败只体现在**末尾那一行** `[...]` 上，而正文里照样
可能夹着 `Traceback`、`Error` 之类字样；反过来，一段正常的 `grep` / 日志输出里
也会出现 "error" 这个词。所以：

1. **先认退出码尾巴**（`[Command failed with exit code N]` / `succeeded`）；
2. 再认 `Error:` **前缀**（大小写一致，与熔断器的口径保持一致，见 MEMORY）；
3. 都不命中才落「成功」。

**绝不**拿关键词在整个正文里做子串匹配 —— 那会把"日志里提到过 error"判成失败，
而误报的代价极高：用户会以为操作没执行成功，转头去重试，把已经做好的事情做砸。

## 返回值

`{"ok": bool | None, "exit_code": int | None, "reason": str}`

- `ok is None` = 判不出来（空输出、纯确认类结果）。前端显示中性状态，
  **不要**硬说是成功。
- `reason` 是给用户看的一句话（≤60 字），失败时用它，成功时通常为空串。
"""

from __future__ import annotations

import re

__all__ = ["classify_tool_result", "outcome_label"]

#: `HostShellBackend` / `OpenSandboxBackend` 的 `_format_execute_output()` 尾巴。
#: ⚠️ 大小写与空格都是契约的一部分（`[Command failed with exit code 1]`）。
_EXIT_CODE_RE = re.compile(r"\[Command (failed|succeeded) with exit code (-?\d+)\]")

#: 失败原因里最长保留多少字符（前端拿到后还会再截一次，这里先收口）
_REASON_LIMIT = 60


def _first_line(text: str, limit: int = _REASON_LIMIT) -> str:
    """取第一行非空内容并截断 —— 失败原因只需要一眼能看懂的那半句。"""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def classify_tool_result(name: str, content: object) -> dict:
    """判定一次工具调用的结果。

    Args:
        name: 工具名（只用于日志/展示，判定规则目前与它无关）。
        content: 工具返回的正文（任何类型都会先 `str()`）。

    Returns:
        见模块 docstring。
    """
    text = str(content if content is not None else "")

    # 1) 退出码尾巴 —— 唯一可信的"命令到底成没成"
    matches = list(_EXIT_CODE_RE.finditer(text))
    if matches:
        verdict, code = matches[-1].group(1), int(matches[-1].group(2))
        if verdict == "failed":
            return {"ok": False, "exit_code": code, "reason": f"退出码 {code}"}
        return {"ok": True, "exit_code": code, "reason": ""}

    # 2) `Error:` 前缀（lstrip 后判），与熔断器 `_is_tool_failure` 同口径
    stripped = text.lstrip()
    if stripped.startswith("Error:"):
        return {"ok": False, "exit_code": None, "reason": _first_line(stripped[6:]) or "工具报错"}

    # 3) 空输出：既不能说成功也不能说失败
    if not text.strip():
        return {"ok": None, "exit_code": None, "reason": "（无输出）"}

    return {"ok": True, "exit_code": None, "reason": ""}


def outcome_label(outcome: dict | None, duration_ms: int | None = None) -> str:
    """把判定结果拍成一句人话（前端也用同一份口径，避免两处各写一套）。

    Examples:
        >>> outcome_label({"ok": True, "exit_code": 0, "reason": ""}, 3200)
        '成功 · 3.2s'
        >>> outcome_label({"ok": False, "exit_code": 1, "reason": "退出码 1"}, 800)
        '失败（退出码 1） · 0.8s'
    """
    o = outcome or {}
    ok = o.get("ok")
    reason = str(o.get("reason") or "")
    if ok is True:
        head = "成功"
    elif ok is False:
        head = f"失败（{reason}）" if reason else "失败"
    else:
        head = "已返回"

    if duration_ms is None:
        return head
    secs = max(int(duration_ms), 0) / 1000
    if secs < 60:
        return f"{head} · {secs:.1f}s"
    return f"{head} · {int(secs // 60)}m{secs % 60:.0f}s"
