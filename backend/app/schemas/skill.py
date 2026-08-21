"""P4 技能接口的请求/响应模型。"""
from pydantic import BaseModel, Field


class CreateSkillRequest(BaseModel):
    name: str = Field(..., description="技能名（小写字母数字+单连字符）")
    description: str = Field(..., description="技能一句话说明")
    instructions: str = Field(..., description="技能完整使用说明（markdown）")
    allowed_tools: list[str] = Field(default_factory=list, description="可选工具白名单")


class CreateSkillResponse(BaseModel):
    name: str
    status: str  # 'draft'：已写盘草稿态，审批后生效
    skill_file: str
