"""三层 Guardrail（P8）——安全边界。

三层：
1. **输入 Guardrail**（`check_input`）：审查用户/上游输入，拦截越权或恶意指令。
2. **步骤 Guardrail**（`check_action`）：工具调用前的敏感操作检查（由中间件/审批配合）。
3. **输出 Guardrail**（`check_output`）：审查模型输出，拦截敏感信息外泄/危险内容。

所有检查返回 `GuardrailResult`：
    - ok=True 表示放行；ok=False 表示拦截，reason 说明原因。
策略是可配置/可扩展的（`SAFETY_RULES`）。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class GuardrailResult:
    __slots__ = ("ok", "reason", "level")

    def __init__(self, ok: bool, reason: str = "", level: str = "deny"):
        self.ok = ok
        self.reason = reason
        self.level = level  # deny / warn / info

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "level": self.level}


# === 规则定义（可扩展） ===

# 输入层：危险的系统/越权指令模式（浅层检测，非完备防线）
INPUT_FORBIDDEN_PATTERNS: list[str] = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"override\s+(your\s+)?system\s+prompt",
    r"forget\s+your\s+instructions",
    r"you\s+are\s+now\s+(an?\s+)?unrestricted",
    r"jailbreak",
    r"\brm\s+-rf\b",
    r"\bformat\s+c[:\s]",
]

# 输出层：疑似敏感信息泄露模式（token/密钥等）
OUTPUT_SENSITIVE_PATTERNS: list[str] = [
    r"(sk-[A-Za-z0-9]{20,})",          # API key 风格
    r"(-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    r"(password(?:\s*[:=]\s*)\S+)",
    r"(api[_-]?key(?:\s*[:=]\s*)\S+)",
]


def _any_match(content: str, patterns: list[str]) -> str:
    for p in patterns:
        if re.search(p, content, flags=re.IGNORECASE):
            return p
    return ""


# === 三层检查 ===

def check_input(user_message: str) -> GuardrailResult:
    """输入 Guardrail：拦截攻击性/越权指令。"""
    if not user_message or not user_message.strip():
        return GuardrailResult(True)
    matched = _any_match(user_message, INPUT_FORBIDDEN_PATTERNS)
    if matched:
        logger.warning("[guardrail] input blocked: matched %r", matched)
        return GuardrailResult(False, f"输入含被拦截的指令模式: {matched}")
    return GuardrailResult(True)


def check_action(tool_name: str, tool_args: dict) -> GuardrailResult:
    """步骤 Guardrail：工具调用前的敏感操作检查。

    这里实现静态规则；真正的"人工审批"（interrupt）由审批流处理敏感/不可判定操作。
    """
    # 写文件到敏感根路径等
    if tool_name in ("write_file", "edit_file", "delete"):
        path = str(tool_args.get("file_path", ""))
        if path and not path.startswith("/"):
            # 要求虚拟 FS 使用绝对路径，防逃逸
            logger.warning("[guardrail] action blocked: non-absolute path %r", path)
            return GuardrailResult(False, f"文件操作需绝对路径: {path}")
    return GuardrailResult(True)


def check_output(model_output: str) -> GuardrailResult:
    """输出 Guardrail：拦截敏感信息外泄。"""
    if not model_output:
        return GuardrailResult(True)
    matched = _any_match(model_output, OUTPUT_SENSITIVE_PATTERNS)
    if matched:
        logger.warning("[guardrail] output flagged: matched %r", matched)
        return GuardrailResult(False, f"输出疑似含敏感信息: {matched}")
    return GuardrailResult(True)
