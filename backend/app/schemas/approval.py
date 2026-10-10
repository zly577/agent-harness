"""审批相关请求/响应模型。"""
from pydantic import BaseModel, Field


class ApprovalAction(BaseModel):
    type: str = "tool_call"
    tool: str = ""
    args: dict | list = Field(default_factory=dict)
    #: 参数的单行摘要（长内容已截断），供界面直接展示
    summary: str = ""
    description: str = ""
    error: str = ""


class ApprovalInterrupt(BaseModel):
    """一个 langgraph 中断及其待审批操作。"""

    #: langgraph 的 `Interrupt.id`（xxh3_128 摘要）；恢复时用它作映射键
    id: str = ""
    actions: list[ApprovalAction] = Field(default_factory=list)


class ApprovalCheckResponse(BaseModel):
    session_id: str
    pending: bool
    #: 全部待审批操作（多个中断时是它们的并集），供弹窗逐条展示
    actions: list[ApprovalAction] = Field(default_factory=list)
    #: 按中断分组的待审批项，含 langgraph 的 interrupt id。
    #: 恢复时必须带上这些 id 才能定向唤醒（一个会话可以同时挂多个中断，
    #: 见 app/agent/approvals.py 模块说明），前端只需原样透传/展示。
    interrupts: list[ApprovalInterrupt] = Field(default_factory=list)


class ApprovalDecideRequest(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$", description="approve 或 reject")


class HitlConfigRequest(BaseModel):
    """热插拔人工介入：enabled=True 开启（敏感写操作需人工同意），False 关闭（全程自动）。

    ## 作用域改成显式字段

    以前 `session_id` 传空就等于"改全局默认"，于是任何用户点一下页头的开关
    都会改掉**所有人**的默认值（关闭时还会把所有挂起的审批一起 reject）。
    多租户下这是跨用户干扰，所以现在把"改哪一层"写成显式参数：

    | 请求 | 作用域 | 谁能用 |
    |------|--------|--------|
    | 带 `session_id` | 该会话 | 会话归属者 |
    | 不带（默认 `scope="user"`） | **自己的默认值** | 任何登录用户 |
    | 不带 + `scope="global"` | 全局默认（影响所有用户） | 仅 admin |

    默认值是 "user" 而不是 "global"，就是为了让"不带参数"这个最普通的调用
    （前端页头那个开关）落到最不容易伤到别人的那一层。
    """

    enabled: bool = Field(..., description="是否启用人工介入")
    session_id: str | None = Field(
        default=None,
        description="只对该会话生效；不传则按 scope 作用于用户默认值或全局默认值",
    )
    scope: str = Field(
        default="user",
        pattern="^(user|global)$",
        description='不带 session_id 时作用于哪一层：user（默认，只影响自己）/ global（仅 admin）',
    )


class HitlConfigResponse(BaseModel):
    enabled: bool
    scope: str
    global_default: bool
    user_overrides: dict[str, bool] = Field(default_factory=dict)
    session_overrides: dict[str, bool] = Field(default_factory=dict)
    #: 本次是否重建了 Agent 缓存（权限是编译期输入，必须重建才生效）
    rebuilt: bool = False
    #: 关闭人工介入时被"放弃收尾"的会话 id。
    #: 前端据此**立刻重载这些会话的历史**：那些悬着的轮次在收尾时补上了回复，
    #: 不刷新的话用户仍然只看到"什么都没有的半轮"。
    cleared: list[str] = Field(default_factory=list)


# 说明：审批决策的响应不再是 JSON 模型 —— POST /api/v1/approvals/{session_id}
# 以 text/event-stream 流回恢复执行的过程（见 app/api/routes/approvals.py），
# 事件格式与 /api/v1/chat/stream 完全一致，前端共用同一套渲染逻辑。
