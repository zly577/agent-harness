"""技能路由（P4）：创建/列出技能。

安全说明：创建技能（写 YAML 定义）是敏感能力，P4 先提供“写盘为草稿态”，
交由人工审批后才纳入运行时（审批激活在 P8 实现）。

P18：**技能定义是系统级资源**（`backend/configs/skills/*.yaml`，所有用户共享同一份），
所以：

- `GET /`（列技能名）任何登录用户都能看 —— 它本来就会出现在每个人的提示词里；
- `POST /`（写技能定义）收紧为**仅管理员**。普通用户能写系统级 YAML 等于能
  给所有人加一段指令，这是越权而不是"功能"。

需求 §3.3.8 明确本期不做技能的用户级隔离，这里就是把"保持全局共享"这句话
落到代码上的那一步。
"""
from fastapi import APIRouter, Depends, HTTPException

from app.agent import skills
from app.api.deps import get_current_admin
from app.db.models import User
from app.schemas.skill import CreateSkillRequest, CreateSkillResponse

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


@router.get("/", response_model=list[str])
def list_skills():
    """列出已可用技能名。"""
    return skills.available_skills()


@router.post("/", response_model=CreateSkillResponse, status_code=201)
def create_skill(
    req: CreateSkillRequest,
    _admin: User = Depends(get_current_admin),
):
    """新增一个技能定义（写为草稿态 YAML，未审批不生效）。**仅管理员。**"""
    try:
        path = skills.create_skill_definition(
            name=req.name,
            description=req.description,
            instructions=req.instructions,
            allowed_tools=req.allowed_tools,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid skill definition: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"create skill failed: {e}")

    return CreateSkillResponse(
        name=req.name, status="draft", skill_file=str(path)
    )
