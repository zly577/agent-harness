"""P4 测试：Skills 系统（YAML 定义/编译/播种/创建），不触发真实模型。"""
import asyncio

import pytest

from app.agent import skills
from app.agent.skills import SkillDefinition


def test_example_skill_definition_valid():
    """示例技能 greet-skill 定义应能通过校验。"""
    defs = skills.load_all_definitions()
    names = [d.name for d in defs]
    assert "greet-skill" in names


def test_invalid_skill_name_rejected():
    with pytest.raises(ValueError):
        SkillDefinition(
            name="Bad Name!",
            description="x",
            instructions="do something",
        )


def test_compile_generates_skil_md():
    """编译 greet-skill 应生成 configs/skills/greet-skill/SKILL.md。"""
    paths = skills.compile_all_skills()
    assert any(p.name == "SKILL.md" and "greet-skill" in str(p) for p in paths)


def test_create_skill_writes_yaml_draft():
    """create_skill_definition 写 YAML 草稿，且校验通过（写到工作区内临时目录）。"""
    import app.agent.skills as sk
    from pathlib import Path
    tmp = Path("tests/_tmp_skill_dir")  # 工作区内，避开系统 Temp 沙箱限制
    tmp.mkdir(parents=True, exist_ok=True)
    orig = sk.DEFINITIONS_DIR
    sk.DEFINITIONS_DIR = tmp
    try:
        p = sk.create_skill_definition(
            name="test-skill",
            description="a test skill",
            instructions="## 用法\n做某事",
            allowed_tools=["read_file"],
        )
        assert p.exists()
        defs = sk.load_all_definitions()
        names = [d.name for d in defs]
        assert "test-skill" in names
    finally:
        sk.DEFINITIONS_DIR = orig
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_seed_skills_into_backend():
    """seed_skills 把技能写入 backend 虚拟文件系统，且 SkillsMiddleware 能扫描。"""
    async def _run():
        from app.agent.skills import compile_all_skills, seed_skills
        from app.agent.sandbox import build_main_backend
        compile_all_skills()
        be = build_main_backend()
        await seed_skills(be)
        r = await be.aread("/skills/greet-skill/SKILL.md")
        content = (r.file_data or {}).get("content", "")
        assert "name: greet-skill" in content
        # 扫描
        metas = await _list_skills(be, "/skills/")
        names = [m.get("name") for m in metas]
        assert "greet-skill" in names
    asyncio.run(_run())


async def _list_skills(backend, path):
    from deepagents.middleware import skills as skmod
    metas, _err = await skmod._alist_skills_with_errors(backend, path)
    return metas


def test_create_skill_api_validation():
    """路由校验：create_skill 非法名返回 400。

    P18：`/api/v1/skills/` 是**系统级**资源（`configs/skills/*.yaml`，所有用户共享），
    所以写操作收紧为**仅管理员**。这里必须用管理员客户端 ——
    用普通用户的 `auth_client` 会先被 403 拦掉，测不到"非法名 → 400"这条校验分支。
    """
    from tests.helpers import make_auth_client, unique_email, create_user

    admin = create_user(email=unique_email("skilladmin"), display_name="技能管理员", role="admin")
    client, _ = make_auth_client(admin, raise_server_exceptions=False)
    resp = client.post("/api/v1/skills/", json={
        "name": "Bad Name!",
        "description": "x",
        "instructions": "y",
    })
    assert resp.status_code == 400
