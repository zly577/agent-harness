"""技能路由（P4）：创建/列出技能。

安全说明：创建技能（写 YAML 定义）是敏感能力，P4 先提供“写盘为草稿态”，
交由人工审批后才纳入运行时（审批激活在 P8 实现）。
"""
from fastapi import APIRouter, HTTPException

from app.agent import skills
from app.schemas.skill import CreateSkillRequest, CreateSkillResponse

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


@router.get("/", response_model=list[str])
def list_skills():
    """列出已可用技能名。"""
    return skills.available_skills()


@router.post("/", response_model=CreateSkillResponse, status_code=201)
def create_skill(req: CreateSkillRequest):
    """新增一个技能定义（写为草稿态 YAML，未审批不生效）。"""
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
