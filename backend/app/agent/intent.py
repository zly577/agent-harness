"""工具调用意图标签（P20）：把「模型在干什么」翻译成一句给用户看的话。

## 为什么需要它

任务看板要回答"现在卡在哪一步"。但看板能拿到的只有工具名（`invoke_get_weather`、
`read_file`），那对用户没有任何意义。所以意图标签必须由**模型**产出：只有它知道
"我为什么要调这个工具"。

## 三条硬边界（这里的每一行都为它们服务）

1. **纯展示层**：意图标签不参与决策、不进工具参数、不改执行顺序。
2. **缺失或非法也必须能跑**：任何一步出问题都退化成"调用 {display_name}"，
   绝不因为一个展示用的字符串让工具调用失败。
3. **不能泄密**：模型可能把参数里的 token / 完整 URL 抄进标签，所以标签要过一遍
   敏感值检测，命中就丢弃并退兜底 —— 宁可少一句说明，也不能把密钥写进前端。

## 这个模块只做纯函数

`resolve_intent()` 是**确定性**的：同样的 `(工具名, 参数)` 一定得到同样的
`(干净参数, 标签, 来源)`。回放同一条 `tool_call` 事件必须得到同样的标签，
所以这里不读时间、不读随机数、不查库、不调 LLM。

## 字段名与契约

`_intent` 是**模型侧**的字段名（前导下划线是刻意的：它不属于任何真实工具的参数
命名空间，不会和业务参数撞名）。落库/下发时它被换成 `intent_label` +
`intent_source`，原始工具参数里**不再保留** `_intent`（见 `strip_intent`）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: 模型侧字段名（schema 里的 properties key）
INTENT_FIELD = "_intent"

#: 标签最大长度（字符）。与 schema 的 `maxLength`、数据契约的 `≤24 字符` 对齐。
INTENT_MAX_CHARS = 24

#: 提示词里建议的长度（比硬上限更短，给模型留余量）
INTENT_PROMPT_CHARS = 20

#: schema 里 `_intent` 的字段说明
INTENT_FIELD_DESCRIPTION = "给用户看的一句话意图"

#: 意图来源：model=模型给的；fallback=目录兜底；backfill=历史回填脚本补的
SOURCE_MODEL = "model"
SOURCE_FALLBACK = "fallback"
SOURCE_BACKFILL = "backfill"


# ============================================================================
# 工具目录：tool_name -> {display_name, icon, category, expected_duration_ms, retryable}
#
# 为什么要有这份目录：
#   1. 兜底文案需要一个"人看得懂的名字"（`调用 read_file` 不如`调用 读取文件`）；
#   2. 前端将来要显示图标/预估耗时/能否重试时，不需要为每个新工具发一次版
#      —— **未收录的工具一律走默认项**，新增工具不会让前端报错。
# 所以这里刻意是"可缺省的表"，不是"必须登记的清单"。
# ============================================================================

_DEFAULT_CATALOG_ENTRY: dict[str, Any] = {
    "display_name": "",  # 空 = 用工具名本身
    "icon": "🔧",
    "category": "other",
    "expected_duration_ms": 0,
    "retryable": False,
}

TOOL_CATALOG: dict[str, dict[str, Any]] = {
    # ---- deepagents 内建：文件与命令 ----
    "ls": {"display_name": "列出目录", "icon": "📁", "category": "filesystem",
           "expected_duration_ms": 300, "retryable": True},
    "read_file": {"display_name": "读取文件", "icon": "📄", "category": "filesystem",
                  "expected_duration_ms": 400, "retryable": True},
    "write_file": {"display_name": "写入文件", "icon": "✍️", "category": "filesystem",
                   "expected_duration_ms": 600, "retryable": False},
    "edit_file": {"display_name": "修改文件", "icon": "✏️", "category": "filesystem",
                  "expected_duration_ms": 600, "retryable": False},
    "delete": {"display_name": "删除文件", "icon": "🗑️", "category": "filesystem",
               "expected_duration_ms": 400, "retryable": False},
    "glob": {"display_name": "查找文件", "icon": "🔍", "category": "filesystem",
             "expected_duration_ms": 800, "retryable": True},
    "grep": {"display_name": "搜索内容", "icon": "🔍", "category": "filesystem",
             "expected_duration_ms": 900, "retryable": True},
    "execute": {"display_name": "执行命令", "icon": "⌨️", "category": "execution",
                "expected_duration_ms": 5000, "retryable": True},
    # ---- deepagents 内建：规划与委派 ----
    "write_todos": {"display_name": "更新任务清单", "icon": "📋", "category": "planning",
                    "expected_duration_ms": 200, "retryable": True},
    "task": {"display_name": "委派子智能体", "icon": "🤝", "category": "delegation",
             "expected_duration_ms": 120000, "retryable": False},
    # ---- 本仓库自带工具 ----
    "tavily_search": {"display_name": "联网搜索", "icon": "🌐", "category": "web",
                      "expected_duration_ms": 6000, "retryable": True},
    "reset_tool_breakers": {"display_name": "重置工具熔断", "icon": "♻️",
                            "category": "maintenance", "expected_duration_ms": 50,
                            "retryable": True},
}


def catalog_entry(tool_name: str) -> dict[str, Any]:
    """取工具目录项；**未收录的工具返回默认项**（display_name 回落到工具名）。

    这是"新增工具不得导致前端发版或报错"的落点：未知工具不会抛 KeyError，
    只是显示名没那么好看。
    """
    entry = TOOL_CATALOG.get((tool_name or "").strip())
    if entry is None:
        entry = {
            **_DEFAULT_CATALOG_ENTRY,
            "display_name": (tool_name or "").strip() or "工具",
        }
    return dict(entry)


def fallback_label(tool_name: str) -> str:
    """目录兜底文案：`调用 {display_name}`（契约里写死的模板）。"""
    return f"调用 {catalog_entry(tool_name)['display_name']}"


# ============================================================================
# 校验：什么样的标签算"非法"
# ============================================================================

#: 控制字符（含换行/回车/制表）—— 单行是硬要求
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

#: 看起来像一次工具调用（`invoke_get_weather(...)` / `read_file (x)`）
_CALL_SHAPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\s*\(.*\)$")

#: 参数名里出现这些词 → 该参数值算敏感
_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|pwd|authorization|bearer|"
    r"credential|cookie|private[_-]?key|access[_-]?key)",
    re.I,
)

#: 值本身长得像凭据（sk-xxx / JWT / 长十六进制）
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"
    r"|[A-Fa-f0-9]{32,})"
)

#: 完整 URL（提示词明确不许抄进标签）
_URL = re.compile(r"https?://[^\s，。；]+")

#: 空转折词：模型偷懒时最爱写的套话（写了等于没写）
_LAZY_PHRASES = ("正在调用工具", "执行任务", "调用工具", "执行操作")


def _mentions_tool_name(label: str, tool_name: str) -> bool:
    """标签里是否"复述了工具名"。

    只对长度 ≥ 4 的工具名生效：`ls` / `rm` 这种两三字符的名字会误伤正常句子
    （"在目录里 ls 一下"其实是合法的自然语言，但这类名字本身也几乎不作为
    模型可见的工具名出现）。
    """
    name = (tool_name or "").strip()
    if len(name) < 4:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", label, re.I
    ) is not None


def _leaks_sensitive(label: str, args: Mapping[str, Any] | None) -> bool:
    """标签里是否抄进了参数中的敏感值 / 完整 URL。

    两条独立的判定：
    1. **参数比对**：参数名像凭据（api_key/token/…）或参数值本身像凭据，
       且该值原样出现在标签里 → 泄漏；
    2. **形状比对**：标签自身带完整 URL 或凭据形状的串 → 泄漏。
    """
    for key, value in (args or {}).items():
        if not isinstance(value, str) or len(value) < 8:
            continue
        sensitive_key = _SENSITIVE_KEY.search(str(key)) is not None
        sensitive_value = _SECRET_VALUE.search(value) is not None
        if (sensitive_key or sensitive_value) and value in label:
            return True
    if _SECRET_VALUE.search(label):
        return True
    url = _URL.search(label)
    return bool(url and ("?" in url.group(0) or len(url.group(0)) > INTENT_MAX_CHARS))


def normalize_intent(
    raw: Any, tool_name: str, args: Mapping[str, Any] | None = None
) -> str | None:
    """把模型给的 `_intent` 规范化；**任何一条不满足就返回 None**（调用方走兜底）。

    Returns:
        规范化后的单行标签（≤ `INTENT_MAX_CHARS`），或 None 表示"丢弃"。
    """
    if not isinstance(raw, str):
        return None
    # 换行/控制字符必须在 strip 之前判：先 strip 会把 "a\\n" 变成 "a" 而放过它
    if _CONTROL.search(raw):
        return None
    label = re.sub(r"\s+", " ", raw).strip()
    if not label:
        return None
    if len(label) > INTENT_MAX_CHARS:
        return None
    if label.casefold() == (tool_name or "").strip().casefold():
        return None
    if INTENT_FIELD in label:
        return None
    first = label[0]
    if first in "{[":
        return None
    try:  # 能把整句解析成 JSON 对象/数组 → 一定是把参数贴上来了
        parsed = json.loads(label)
        if isinstance(parsed, (dict, list)):
            return None
    except Exception:  # noqa: BLE001 不是 JSON 就是好事
        pass
    if _CALL_SHAPE.match(label):
        return None
    if any(p in label for p in _LAZY_PHRASES):
        return None
    if _mentions_tool_name(label, tool_name):
        return None
    if _leaks_sensitive(label, args):
        return None
    return label


# ============================================================================
# 剥离 + 解析（**集中在这一处**，不要在工具实现里手写）
# ============================================================================

def strip_intent(args: Any) -> tuple[dict[str, Any], Any]:
    """从参数里摘除 `_intent`，返回 `(其余参数, 原始 _intent 值)`。

    `_intent` 只存在于"模型 → 我们"这一段，**绝不能**往下传到
    工具实现、更不要说外部 API（MCP server / Tavily）。所有下发路径
    （工具执行、事件流、落库、审批）都必须先过这里。

    非 dict 参数原样返回（工具的 args 理论上总是 dict，但这里不做假设，
    因为它的失败代价是"整个工具调用炸掉"，而收益只是省两行代码）。
    """
    if not isinstance(args, Mapping):
        return {}, None
    raw = args.get(INTENT_FIELD)
    clean = {k: v for k, v in args.items() if k != INTENT_FIELD}
    return clean, raw


def resolve_intent(
    tool_name: str, args: Any
) -> tuple[dict[str, Any], str, str]:
    """一次拿到 `(干净参数, intent_label, intent_source)` —— 唯一的入口。

    - 合法 → `(clean, 标签, "model")`
    - 缺失/非法/超长/换行/像 JSON/像工具名/含敏感值 → `(clean, "调用 X", "fallback")`

    `intent_label` **永远不会是 None**：看板在任何情况下都得有东西可显示。
    """
    clean, raw = strip_intent(args)
    label = normalize_intent(raw, tool_name, clean)
    if label is None:
        return clean, fallback_label(tool_name), SOURCE_FALLBACK
    return clean, label, SOURCE_MODEL


def deterministic_label(tool_name: str, args: Any) -> str:
    """回填脚本用的**确定性**标签（不调 LLM，只看工具名 + 参数）。

    先试目录兜底，再把参数里最有信息量的一个值附上（如 `调用 读取文件` 变成
    `读取 main.py`），仍然不合法（超长/含敏感值/复述工具名）时退回纯兜底文案。
    同一 `(工具名, 参数)` 永远得到同一结果 —— 回填可重复执行且不漂移。
    """
    clean, _ = strip_intent(args)
    hint_key, hint_value = _best_hint(clean)
    if hint_value:
        # 中英之间的分隔：`读取 main.py` 而不是 `读取main.py`；
        # 但 `搜索北京天气` 不该被插一个空格（纯中文之间加空格是错的排版）
        sep = " " if hint_value[:1].isascii() else ""
        candidate = f"{_verb_for(tool_name)}{sep}{hint_value}"
        label = normalize_intent(candidate, tool_name, clean)
        if label is not None:
            return label
    return fallback_label(tool_name)


#: 参数里的"亮点"优先级：越靠前越能说明这一步在做什么
_HINT_KEYS = (
    "query", "file_path", "path", "pattern", "command", "subagent_type",
    "description", "url", "name",
)

#: 工具名 → 动词（回填文案用）。未收录的工具用"调用"。
_VERB_OVERRIDES: dict[str, str] = {
    "ls": "列出",
    "read_file": "读取",
    "write_file": "写入",
    "edit_file": "修改",
    "delete": "删除",
    "glob": "查找",
    "grep": "搜索",
    "execute": "执行",
    "task": "委派",
    "tavily_search": "搜索",
    "reset_tool_breakers": "重置",
    "write_todos": "更新",
}


def _verb_for(tool_name: str) -> str:
    verb = _VERB_OVERRIDES.get((tool_name or "").strip())
    return verb or (catalog_entry(tool_name)["display_name"] or "调用")


def _best_hint(args: Mapping[str, Any]) -> tuple[str, str]:
    """从参数里挑一个最像"目标"的短值（只取最后一段，避免长路径撑爆长度）。"""
    for key in _HINT_KEYS:
        value = args.get(key)
        if not isinstance(value, str):
            continue
        text = " ".join(value.split())
        if not text:
            continue
        # 路径只取文件名：`/mnt/d/proj/main.py` → `main.py`
        text = re.split(r"[\\/]", text)[-1] or text
        if len(text) > 16:
            continue
        if _SENSITIVE_KEY.search(key) or _SECRET_VALUE.search(text):
            continue
        if key == "description" and len(text) > 12:
            continue
        return key, text
    return "", ""


# ============================================================================
# 提示词段落（第 1 层）
# ============================================================================

_INTENT_SECTION_ZH = """## 工具调用意图标签（`_intent`）
每次调用工具时，必须同时在 `_intent` 字段给出一句给终端用户看的说明：
- 用现在进行时，{limit} 字以内，单行，不加引号
- 说"在做什么 / 为什么要做"，不要复述工具名
- 不得包含参数中的敏感值（密钥、token、个人数据、完整 URL）
- 不要写"正在调用 X 工具"这类套话
- 用用户提问的语言写

正例：查询北京今天的天气 / 为该订单创建退款单 / 读取 user_profile.py 的前 50 行
反例：invoke_get_weather(...) / 正在调用工具 / 执行任务

`_intent` 只是给用户看的说明，不是工具的输入：不要把它当成参数、也不要写进别的字段。
它缺失时系统会自动补一句兜底文案，但你仍然应该在每次调用时都给出它。
"""

_INTENT_SECTION_EN = """## Tool-call intent label (`_intent`)
Every tool call must also carry `_intent`: one short line describing, for the end
user, what you are doing right now.
- Present continuous, at most {limit} characters, single line, no quotes
- Say what you are doing / why; never restate the tool name
- Never include sensitive argument values (secrets, tokens, personal data, full URLs)
- Never write filler such as "calling tool X"
- Write it in the language the user is using

Good: `Checking today's weather in Beijing` / `Reading the first 50 lines of user_profile.py`
Bad: `invoke_get_weather(...)` / `calling tool` / `executing task`

`_intent` is a note for the user, not a tool input: do not treat it as an argument
and do not put it into other fields. The system supplies a fallback when it is
missing, but you should still provide it on every call.
"""

_SECTIONS = {"zh": _INTENT_SECTION_ZH, "en": _INTENT_SECTION_EN}


def normalize_locale(locale: str | None) -> str:
    """把任意 locale 串收敛成支持的两种之一（默认中文）。

    支持 `zh*` / `en*`；其它一律当 zh（本项目默认中文用户）。
    """
    value = (locale or "").strip().lower()
    return "en" if value.startswith("en") else "zh"


def render_intent_prompt_section(locale: str | None = None) -> str:
    """第 1 层：拼出提示词段落（结束带空行，方便直接追加）。"""
    template = _SECTIONS[normalize_locale(locale)]
    return "\n" + template.format(limit=INTENT_PROMPT_CHARS) + "\n"


__all__ = [
    "INTENT_FIELD",
    "INTENT_FIELD_DESCRIPTION",
    "INTENT_MAX_CHARS",
    "INTENT_PROMPT_CHARS",
    "SOURCE_BACKFILL",
    "SOURCE_FALLBACK",
    "SOURCE_MODEL",
    "TOOL_CATALOG",
    "catalog_entry",
    "deterministic_label",
    "fallback_label",
    "normalize_intent",
    "normalize_locale",
    "render_intent_prompt_section",
    "resolve_intent",
    "strip_intent",
]
