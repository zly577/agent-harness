"""Skills 系统（P4）。

设计目标（贴合用户“用 YAML 配置新 skills，并可由 Agent 创建”）：
- 用户以**纯 YAML 文件**声明一个技能（更友好、可读、可审查），放在
  `backend/configs/skills/definitions/<name>.yaml`。
- 系统把 YAML 定义**编译**成 deepagents 原生技能目录 `configs/skills/<name>/SKILL.md`
  （带 YAML frontmatter），复用 deepagents 的 `SkillsMiddleware` 加载。
- 启动时把技能文件**播种**进 backend 的虚拟文件系统（/skills/），供
  `SkillsMiddleware(sources=[...])` 扫描。
- Agent 创建技能：P4 提供 `create_skill` 支持（生成 YAML 定义，草稿态），
  激活仍需人工审批（P8 起强制）。

YAML 定义结构（definitions/<name>.yaml）:
    name: greet-skill           # 小写字母数字+单连字符
    description: ...            # 一句话说明
    allowed_tools: []           # 可选：允许的工具白名单
    instructions: |              # 技能的完整使用说明（markdown）
        ...
"""
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# 技能相关路径（backend/configs/skills）
_BASE = Path(__file__).resolve().parent.parent.parent / "configs" / "skills"
DEFINITIONS_DIR = _BASE / "definitions"   # YAML 定义
COMPILED_DIR = _BASE  # 编译后的技能目录（<name>/SKILL.md）
BACKEND_SKILLS_ROOT = "/skills/"           # backend 虚拟文件系统里的技能根


class SkillDefinition(BaseModel):
    """一个 YAML 定义的技能（Pydantic 校验）。"""

    name: str
    description: str
    allowed_tools: list[str] = Field(default_factory=list)
    instructions: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v or not (all(
            c.isascii() and (c.islower() or c.isdigit() or c == "-")
            for c in v
        ) and not v.startswith("-") and not v.endswith("-") and "--" not in v):
            raise ValueError(
                "name 必须为小写字母/数字/单连字符，长度 1-64，不能以 - 开头或结尾"
            )
        return v

    @field_validator("instructions")
    @classmethod
    def _validate_instructions(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("instructions 不能为空")
        return v


def _render_skill_md(defn: SkillDefinition) -> str:
    """把 YAML 定义编译成 deepagents 原生 SKILL.md（YAML frontmatter + markdown）。"""
    fm = {
        "name": defn.name,
        "description": defn.description,
        "allowed-tools": defn.allowed_tools,
    }
    head = "---\n" + "\n".join(f"{k}: {_yaml_scalar(v)}" for k, v in fm.items()) + "\n---\n"
    body = f"# {defn.name}\n\n{defn.instructions.strip()}\n"
    return head + body


def _yaml_scalar(v):
    """简单标量序列化；列表用 YAML 内联。"""
    if isinstance(v, list):
        return "[" + ", ".join(f'"{x}"' for x in v) + "]"
    if isinstance(v, str):
        return _quote(v)
    return str(v)


def _quote(s: str) -> str:
    # 含特殊字符则双引号包裹并转义
    if any(c in s for c in ':\n"#[]{}\t'):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def load_all_definitions() -> list[SkillDefinition]:
    """读取 definitions/ 下所有 YAML 定义并做 Pydantic 校验。"""
    result: list[SkillDefinition] = []
    if not DEFINITIONS_DIR.exists():
        return result
    for f in sorted(DEFINITIONS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            result.append(SkillDefinition(**data))
        except Exception as e:  # 校验失败跳过该技能，但记录
            raise RuntimeError(f"技能定义 {f.name} 校验失败: {e}") from e
    return result


def create_skill_definition(name: str, description: str, instructions: str,
                            allowed_tools: list[str] | None = None) -> Path:
    """把一个新的技能定义写入 definitions/ 下的 YAML（草稿态）。

    仅写盘，不自动激活；激活需人工审批（P8 起）。返回写入的 YAML 路径。
    """
    defn = SkillDefinition(
        name=name, description=description, instructions=instructions,
        allowed_tools=allowed_tools or [],
    )
    DEFINITIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = DEFINITIONS_DIR / f"{defn.name}.yaml"
    doc = {
        "name": defn.name,
        "description": defn.description,
        "allowed_tools": defn.allowed_tools,
        "instructions": defn.instructions,
    }
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def compile_all_skills() -> list[Path]:
    """把 definitions 里的所有技能编译为 configs/skills/<name>/SKILL.md。"""
    written: list[Path] = []
    for defn in load_all_definitions():
        skill_dir = COMPILED_DIR / defn.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        md_path = skill_dir / "SKILL.md"
        md_path.write_text(_render_skill_md(defn), encoding="utf-8")
        written.append(md_path)
    return written


async def seed_skills(backend) -> None:
    """把已编译的技能目录播种进 backend 虚拟文件系统，供 SkillsMiddleware 扫描。"""
    from deepagents.backends.protocol import PERMISSION_DENIED, INVALID_PATH

    for skill_dir in COMPILED_DIR.iterdir():
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").exists():
            continue
        rel = skill_dir.name
        for f in skill_dir.rglob("*"):
            if not f.is_file():
                continue
            virt = f"{BACKEND_SKILLS_ROOT}{rel}/{f.relative_to(skill_dir).as_posix()}"
            try:
                await backend.awrite(virt, f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass  # 单个文件失败不中断


def build_skills_middleware(backend):
    """构建 deepagents SkillsMiddleware，扫描 backend 里的 /skills/。"""
    from deepagents.middleware.skills import SkillsMiddleware
    return SkillsMiddleware(backend=backend, sources=[BACKEND_SKILLS_ROOT])


@lru_cache
def available_skills() -> list[str]:
    """返回已编译/可用的技能名列表。"""
    return [d.name for d in load_all_definitions()]
