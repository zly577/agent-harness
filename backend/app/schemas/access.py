"""权限模式（沙箱内 / 完全访问）的请求与响应模型。

字段说明与作用域规则见 `app/agent/access.py` —— 这里的三个字段和
`HitlConfigRequest` 是同一套设计（`session_id` / `scope` 两段式），
刻意保持对称，读代码时不需要切换心智模型。
"""
from pydantic import BaseModel, Field


class AccessConfigRequest(BaseModel):
    """切换权限模式。

    | 请求 | 作用域 | 谁能用 |
    |------|--------|--------|
    | 带 `session_id` | 只影响该会话 | 会话归属者 |
    | 不带（默认 `scope="user"`） | **自己的默认值** | 任何登录用户 |
    | 不带 + `scope="global"` | 全局默认（影响所有用户） | 仅 admin |

    ⚠️ `mode` 没有写 `pattern` 约束，而是交给路由用 `access.normalize_mode()`
    校验 —— 合法的取值只有一处定义（`access.VALID_MODES`），
    在 schema 里再抄一份字面量迟早会漂。非法值路由会返回 400（而不是 422），
    错误信息里会带上可选值。
    """

    mode: str = Field(..., description='权限模式：`"sandbox"`（默认权限，沙箱内）或 `"full"`（完全访问）')
    session_id: str | None = Field(
        default=None,
        description="只对该会话生效；不传则按 scope 作用于用户默认值或全局默认值",
    )
    scope: str = Field(
        default="user",
        pattern="^(user|global)$",
        description='不带 session_id 时作用于哪一层：user（默认，只影响自己）/ global（仅 admin）',
    )


class AccessConfigResponse(BaseModel):
    """当前权限模式的状态（含生效层级，便于前端解释"为什么我切了却像没生效"）。"""

    #: `"sandbox"` | `"full"`
    mode: str
    #: 中文名，前端直接展示，避免各处自己拼文案
    label: str = ""
    #: 便捷布尔（等价于 `mode == "full"`）
    full_access: bool = False
    #: 当前生效层级：session / user / global
    scope: str = "global"
    #: 全局默认值（.env 的 HARNESS_ACCESS_MODE）
    global_default: str = "sandbox"
    user_overrides: dict[str, str] = Field(default_factory=dict)
    session_overrides: dict[str, str] = Field(default_factory=dict)
    #: 合法取值，供前端渲染选项
    valid_modes: list[str] = Field(default_factory=list)
    #: 本次是否重建了 Agent 缓存（backend/permissions 是编译期输入，必须重建才生效）
    rebuilt: bool = False
    #: 切到完全访问时被"放弃收尾"的会话 id（挂起的审批已无法再生效）。
    #: 前端据此立刻重载这些会话的历史，否则用户看到的还是那半轮。
    cleared: list[str] = Field(default_factory=list)
