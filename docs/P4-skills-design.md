# P4 Skills 系统设计

## 目标

支持"用 YAML 配置新 skills，并可由 Agent 创建"，复用 deepagents 原生 Skills 机制，不造轮子。

## 架构

```
backend/configs/skills/
├── definitions/<name>.yaml   # ★ 用户用 YAML 声明技能（name/description/allowed_tools/instructions）
├── <name>/SKILL.md           # 自动编译产物（deepagents 原生格式：YAML frontmatter + markdown）
│   └── (helper 文件可选)

运行时：
  compile_all_skills()        # YAML → SKILL.md
  seed_skills(backend)        # SKILL.md → backend 虚拟文件系统 /skills/
  SkillsMiddleware(sources=["/skills/"])   # deepagents 扫描加载
  → 技能元数据注入 system prompt；模型用 read_file 按需读技能详情
```

## 用户如何加技能（两种）

1. **手动写 YAML**：在 `configs/skills/definitions/<name>.yaml` 写一个纯 YAML 定义，
   启动时自动编译并生效。
2. **API 创建**：`POST /api/v1/skills/` 传 name/description/instructions/allowed_tools，
   写为草稿态 YAML（P4 不自动激活，审批激活在 P8）。

## 关键实现

- `app/agent/skills.py`：
  - `SkillDefinition`：Pydantic 校验（name 规范、instructions 非空）
  - `_render_skill_md()`：YAML 定义 → 标准 SKILL.md（frontmatter + 正文）
  - `load_all_definitions()`：读取并校验 definitions/
  - `create_skill_definition()`：写草稿 YAML
  - `compile_all_skills()`：编译到 configs/skills/<name>/SKILL.md
  - `seed_skills(backend)`：播种进 backend 虚拟文件系统
  - `build_skills_middleware(backend)`：返回 deepagents SkillsMiddleware
- `app/agent/runtime.py`：`get_agent()` 里 compile + seed + 挂 SkillsMiddleware。
- `app/api/routes/skills.py`：GET /（列出技能）、POST /（创建草稿）。
- 示例技能：`definitions/greet-skill.yaml`（已编译并验证生效）。

## 安全

- 技能名强校验（小写字母/数字/单连字符），防路径注入。
- Agent 创建技能 → 草稿态，需人工审批才生效（P8 实现强制审批）。
- 技能文件经 backend 虚拟文件系统（InMemory Store），不直接落真盘执行指令。

## 验证结果

- 模型端到端响应 greet-skill（热情问候+自我介绍+询问）。
- available_skills() 返回 ['greet-skill']。
- pytest：16 passed。

## 待办

- P8：create_skill 的人工审批流（审批后 compile+seed 激活）。
- 可扩展：技能支持 helper 脚本（allowed_tools 白名单）。
