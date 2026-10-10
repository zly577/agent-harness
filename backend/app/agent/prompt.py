"""智能体的系统提示词与「长期记忆」（Agent.md）。

## 两个通道的分工

- **system_prompt（代码内，稳定）**：框架级规则（工具用法、委派规范、路径约定），
  放在这里保证任何时候都生效，不依赖磁盘状态。
- **memory（Agent.md，可变）**：跨会话的偏好与经验。deepagents 的 `MemoryMiddleware`
  会在**每次运行开始时**读取该文件、注入到系统提示里，并明确告诉模型
  "可以用 `edit_file` 更新它" —— 所以智能体能自己记住用户的偏好。

## 为什么用文件而不是数据库

1. 用户能直接看、直接改（就是一篇文章），也可版本管理；
2. 模型用现成的 `edit_file` 工具就能维护，不需要新造一个"记忆工具"；
3. 与工作区文件同一套权限（`/workspace/**`、以及本机挂载点 `/mnt/**` 的写操作
   都会走人工审批）。

## 路径约定

`/workspace/Agent.md` → 本地 `<repo>/workspace/Agent.md`，也就是容器里的
`/workspace/Agent.md` —— 三个视角同一个文件（见 sandbox.build_main_backend 的说明）。

P12（`HARNESS_MACHINE_ACCESS=true`）时，提示词里会额外声明本机磁盘挂载点
`/mnt/<盘符>/...`（= 用户电脑的真实文件，**不进沙箱**），见
`machine_mount_prompt_section()`。

末尾还会追加「工具调用意图标签」段落（`_intent`），见
`app/agent/intent.py::render_intent_prompt_section` —— 它让模型每次调工具时
都产出一句面向终端用户的说明，供任务看板的"当前步骤"展示。

## 提示词结构按「权限模式」二选一

```
    _PROMPT_HEAD  +  _FS_TOOLS_SANDBOX|_FS_TOOLS_FULL  +  _PROMPT_TAIL
                  +  machine_mount_prompt_section() | full_access_prompt_section()
```

两套「文件与命令工具」段是**互斥**的（`build_system_prompt(mode=...)` 负责选），
因为它们的核心事实相反：默认权限说"execute 在隔离容器里"，完全访问说"execute
就在本机"。让模型同时读到两种说法 = 它只能靠猜，这是历史上最贵的一类提示词 bug
（三套路径各试一遍、单次任务空转 51 次）。

## 刷新时机

`MemoryMiddleware.abefore_agent` 只在 `state["memory_contents"]` 不存在时读取，
而该字段随 thread 存档；因此**同一会话的后续轮次不会重读**（避免每轮都变提示词、
破坏缓存），但**每个新会话都会读到最新内容**。智能体在同一会话里更新记忆后，
新会话立即生效。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: 模型看到的记忆文件虚拟路径（= 容器里的路径）
AGENT_MEMORY_PATH = "/workspace/Agent.md"

#: 工具使用原则 + 子智能体委派 —— 两种权限模式共用的头部。
_PROMPT_HEAD = (
    "你是 Agent Harness，一个运行在 DeepAgents 框架上的通用智能体助手。"
    "用中文回答，简洁准确，不说废话。\n\n"
    "## 工具使用原则\n"
    "- 简单常识、闲聊、能直接回答的问题：直接回答，不要调用任何工具。\n"
    "- 需要查信息、读文件、写文件、跑命令、处理复杂任务：主动调用工具，不要只给建议。\n"
    "- 需要进行联网搜索就调用 tavily_search 工具。\n"
    "- 调用工具前无需向用户确认，直接执行；完成后用自然语言汇报结果。\n\n"
    "## 执行透明（每一步都要能对上账）\n"
    "用户看不到你的工具调用细节，只能看到工具卡上那一行字。所以：\n"
    "- **每执行完一个会改动世界的动作**（跑命令、写文件、改文件、删文件、装依赖），"
    "紧接着用**一句话**说清：做了什么、作用在哪个对象上、**成功还是失败**。"
    "失败就直说失败并给出关键报错，不要含糊成「已处理」。\n"
    "- 一轮结束时，如果本轮执行过多个动作，用一个简短清单收尾（每条一行），"
    "让人一眼看出「哪几步成了、哪一步没成」。没执行任何动作就不用写清单。\n"
    "- 只汇报**你确实做过的事**：没执行成功的不要说成成功，没验证过的不要说成已验证。\n\n"
    "## ⚠️ 危险命令红线（会导致整轮对话直接消失）\n"
    "- **绝不**用「按进程名批量杀进程」的方式重启服务 —— `Stop-Process -Name python -Force`、"
    "`taskkill /IM python.exe /F`、`pkill python` 这类命令会把**本机所有 python 进程**杀光，"
    "而运行这次对话的后端服务本身就是其中一个：后端一死，这一轮永久没有结果，"
    "用户界面会一直停在「进行中」等到放弃。\n"
    "- 要重启某个服务，先按**端口**定位到唯一的 PID，再只结束那一个进程：\n"
    "  · `netstat -ano | findstr :9000` → 拿到最后一列的 PID\n"
    "  · `Stop-Process -Id <PID> -Force`\n"
    "- 同理，不要结束你自己不认识的进程；不确定就先报告给用户，让用户决定。\n\n"
    "## 子智能体委派（task 工具）—— 复杂任务必须用\n"
    "当任务满足以下任一条件时，**必须调用 task 工具**委派给子智能体，不要自己硬扛：\n"
    "- 多步骤、需要深度调研或综合分析的任务\n"
    "- 需要大量独立思考空间、上下文较重的任务\n"
    "- 你预估直接处理会超过 3 轮工具调用的任务\n"
    "- 用户明确要求「调研」「分析」「整理」「研究」的任务\n\n"
    "task 工具参数填写规范：\n"
    "- subagent_type：从系统提供的可用子智能体列表中选最合适的一个\n"
    "- description：**必须自包含**，写清任务目标、已知上下文、约束条件、期望输出格式。"
    "子智能体看不到对话历史，所以所有背景都要写在这里。\n\n"
    "子智能体在隔离上下文中运行，返回最终报告后由你整合并回复用户。\n"
    "可以在同一条消息中发起多个独立的 task 调用（并行委派）。\n\n"
)

#: 文件 / 命令工具段 —— **默认权限（沙箱内）**。
#: 完全访问模式下整段被 `_FS_TOOLS_FULL` **替换**（两段互斥）。
#: 这一点很关键 —— 若两段同时出现，模型会同时读到"execute 在容器里跑"和
#: "execute 在本机跑"，只能靠猜（历史教训：BASE_SYSTEM_PROMPT 自相矛盾曾导致
#: 模型三套路径各试一遍、空转 51 次，见 `_probe/probe_prompt_path_rules.py`）。
_FS_TOOLS_SANDBOX = (
    "## 文件与命令工具（两种执行位置，务必分清）\n"
    "- 读文件：read_file；搜文件：glob；搜内容：grep；列目录：ls\n"
    "- 写文件：write_file；改文件：edit_file\n"
    "- 上述文件工具直接读写**用户电脑上的真实文件**（不经过沙箱、不需要沙箱在线）：\n"
    "  · 用户本机的真实位置 → 用 `/mnt/<盘符>/...`（转换规则见下文「运行环境」）\n"
    "  · 交付物、临时脚本 → 用 `/workspace/...`（持久保存，用户能直接看到并一键下载）\n"
    "- 执行 shell 命令：execute。命令在**隔离沙箱容器**内运行，容器工作目录同样是 /workspace，"
    "需要跑代码、装依赖、做数据分析时才用它。\n"
    "- 沙箱与本地工作目录会自动同步：你在 /workspace 里写的文件会在 execute 前自动送进沙箱；"
    "沙箱里新建/改动的文件会在执行结束后自动同步回本地。你不需要手工搬运文件。\n"
    "- ⚠️ 沙箱容器**看不到 `/mnt/*`**：绝不要用 execute 去查看、搜索用户本机上的文件。\n\n"
)

#: 文件 / 命令工具段 —— **完全访问（execute 直接在本机跑）**。
#:
#: 与沙箱段的三处关键差异（都不能省，每一处都对应一类实际踩过的坑）：
#: 1. `execute` **不再进容器**，就在这台电脑上跑；
#: 2. 文件工具用**虚拟路径**（`/mnt/...`），而 execute 用 **Windows 真实路径** ——
#:    混淆这两者正是历史上"空转几十次"的主因，必须写死；
#: 3. 系统是 Windows / cmd，Unix 惯用命令（`ls -la`、`cat`、`python3`）会直接报错。
_FS_TOOLS_FULL = (
    "## 文件与命令工具（完全访问模式：你就是跑在这台电脑上，没有沙箱）\n"
    "- 读文件：read_file；搜文件：glob；搜内容：grep；列目录：ls\n"
    "- 写文件：write_file；改文件：edit_file\n"
    "- ⚠️ 文件工具的路径是**虚拟路径**，不是 Windows 真实路径：\n"
    "  · 用户本机的位置 → `/mnt/<盘符>/...`（`D:\\proj\\a.py` → `/mnt/d/proj/a.py`）\n"
    "  · 交付物、临时脚本 → `/workspace/...`（持久保存，用户能直接看到并一键下载）\n"
    "- 执行 shell 命令：execute。**命令直接在这台电脑上运行**（没有容器、没有隔离），"
    "工作目录就是你的工作区目录，可以直接用相对路径跑脚本。\n"
    "- ⚠️ execute 的命令里用 **Windows 真实路径**（`D:\\proj\\a.py` 或 `D:/proj/a.py`），"
    "不要写 `/mnt/d/...` 或 `/workspace/...` —— 那两个是给文件工具的虚拟路径，命令行不认。\n"
    "- ⚠️ 系统是 Windows、shell 是 cmd：用 `dir` / `type` / `python xx.py`，"
    "不要用 `ls -la`、`cat`、`python3`、用单引号包住整条命令这些 Unix 惯用法。\n"
    "- 命令输出会原样返回；失败时末尾会带 `Exit code: N`，据此判断是否成功。\n\n"
)

#: 长期记忆段 —— 两种模式共用。
_PROMPT_TAIL = (
    "## 长期记忆（Agent.md）\n"
    f"- 你的长期记忆文件是 `{AGENT_MEMORY_PATH}`，它已在启动时加载进这段提示词。\n"
    "- 当用户表达偏好、纠正你的做法、或提供以后用得上的信息时，"
    "**主动用 edit_file 更新该文件**（写入「用户偏好」小节），不要只在本轮口头答应。\n"
    "- 只记**长期有用**的内容（偏好、习惯、约定、项目背景）；"
    "一次性任务、临时状态、闲聊不要写入。\n"
    "- **绝不**把 API key、密码、token 等凭据写进记忆。"
)

#: 框架级系统提示词（**默认权限（沙箱内）**版本；稳定，不放记忆内容）。
#: 保留这个常量是为了兼容旧引用（探针/测试）；真正构建 Agent 时应调用
#: `build_system_prompt(mode=...)`，它会按权限模式选用不同的工具说明段。
BASE_SYSTEM_PROMPT = _PROMPT_HEAD + _FS_TOOLS_SANDBOX + _PROMPT_TAIL

#: Agent.md 首次生成时的种子内容（用户与智能体都可以直接改）
SEED_MEMORY = f"""# Agent 记忆

> 这是 Agent Harness 的长期记忆文件（`{AGENT_MEMORY_PATH}`）。
> 每次新会话开始时，它的内容会被加载进系统提示词。
> 智能体会在了解你的偏好后用 `edit_file` 自行更新它 —— 你也可以直接改这个文件。

## 用户偏好

<!-- 记录稳定的偏好，例如：回答语言、篇幅、代码风格、常用工具链 -->
- 回答语言：中文
- 篇幅：简洁，先给结论再给理由
- 产出文件统一放在 `/workspace/` 下

## 环境与约定

- 默认工作环境就是这台电脑：文件工具直接读写真实文件，本机路径用 `/mnt/<盘符>/...`（如 `/mnt/d/...` = D 盘）
- 交付物、临时脚本写 `/workspace/`（持久保存，可一键下载）
- 只有 `execute`（跑代码）进隔离沙箱；容器只镜像 `/workspace`，**看不到 `/mnt/*`**
- 复杂/调研类任务用 `task` 委派给子智能体，description 必须自包含

## 经验与项目背景

<!-- 记录踩过的坑、项目结构、术语约定等 -->

## 待确认

<!-- 还没定下来的事，问清楚后可以挪到上面的小节 -->
"""


def memory_file_path(user_id: str | None = None) -> Path:
    """本地磁盘上 Agent.md 的真实路径（= **该用户**工作区根下的 Agent.md）。

    按用户隔离：`<base>/<user_id>/Agent.md`。
    这是"长期记忆不泄漏"的关键落点 —— 以前它是全用户共享的一篇文章，
    A 记下的偏好会直接进 B 的系统提示词。
    """
    from app.core.workspace import workspace_for

    return workspace_for(user_id) / "Agent.md"


def ensure_memory_file(user_id: str | None = None) -> Path:
    """确保该用户的 Agent.md 存在（不存在就用种子内容创建），返回其路径。

    幂等：已存在则**绝不覆盖**（那会抹掉智能体积累的记忆）。
    """
    path = memory_file_path(user_id)
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SEED_MEMORY, encoding="utf-8")
            logger.info("[memory] 已创建长期记忆文件: %s", path)
    except OSError as e:  # 磁盘异常不能让建 Agent 失败
        logger.warning("[memory] 创建 %s 失败: %s", path, e)
    return path


def machine_mount_prompt_section() -> str:
    """本机磁盘挂载点的提示词段落（2026-09-30 重写）。

    `HARNESS_MACHINE_ACCESS=true` 时列出**这台电脑实际挂载进来的盘符/目录**，
    并交代三件模型必须知道的事：

    1. **路径转换规则**（`D:\\proj\\a.py` → `/mnt/d/proj/a.py`）。旧版只说
       "用户提到本机/D 盘时用 `/mnt/<盘符>/...`"，**从没说过 Windows 路径该怎么翻译** ——
       于是模型手上同时存在 `/workspace/`、`/mnt/d/...`、`D:\\...` 三套写法，
       只能一个个试（实测一次任务里三套各试了 19+20+12 次）。
    2. **默认就在本机工作**：用户提本机文件时，第一反应必须是 `/mnt/...`，
       而不是先去 `/workspace/` 里翻。
    3. **`execute` 不是找文件的工具**：容器只有 `/workspace` 的镜像、看不到 `/mnt/*`，
       用 execute 去 ls 本机会拿到空目录，然后误判"文件不存在"。

    没开启时返回空串（提示词与完全一致）。
    """
    from app.agent.opensandbox_backend import machine_mounts

    mounts = machine_mounts()
    if not mounts:
        return ""

    lines = [
        "\n## 运行环境：你的默认工作目录就是用户这台电脑（不是沙箱）\n",
        "文件工具（read_file / ls / glob / grep / write_file / edit_file / delete）"
        "**直接读写用户电脑上的真实文件**，不经过沙箱、不需要同步。"
        "**只有 `execute`（跑命令/代码）才叫「进沙箱」** —— 那时才会进入隔离容器。\n\n",
        "### 路径对照（用户说的 → 你调用工具时写的）\n",
    ]
    for name, path in mounts:
        lines.append(
            f"- `/mnt/{name}/` = `{path}`（`/mnt/{name}/a/b.py` 就是 `{path}` 下的 a/b.py）\n"
        )
    lines.append(
        "- 用户说「D 盘 / 我的项目 / 桌面 / 某个目录」时，把盘符段换成 `/mnt/<小写盘符>/`，"
        "其余反斜杠一律写成 `/`。例如 `D:\\proj\\a.py` → `/mnt/d/proj/a.py`。\n"
        "- 没有明确路径、或者说「帮我做个报告」时，用 `/workspace/...`"
        "（工作区；交付物落这里，用户能一键下载）。\n"
        "- `ls /mnt` 可以看当前挂了哪些盘。\n\n"
        "### 硬性要求\n"
        "1. 用户提到本机文件，**第一反应就是 `/mnt/...`，直接读**，不要先去 `/workspace` 里翻。"
        "他说「读一下 D:\\proj\\README.md」，你就 `read_file(\"/mnt/d/proj/README.md\")`，一步到位。\n"
        "2. **绝不要为了「找文件」而调用 `execute`。** 容器里只有 `/workspace` 的镜像、看不到 `/mnt/*`；"
        "用 execute 去 ls/cat 本机文件只会得到空目录，然后你会误判「文件不存在」。\n"
        "3. 路径不完整时先用 `ls` 看清楚（如 `ls /mnt/d/`）再往下走，不要凭空猜路径。\n"
        "4. 用 `glob` / `grep` 搜索**必须带 `path=`**（如 `path=/mnt/d/proj`）；"
        "不带 path 会扫全盘，又慢又容易被 5 秒超时砍成空结果。\n"
        "5. 只有真正要**执行代码 / 装依赖 / 跑脚本**时才用 `execute`；"
        "要执行的脚本先写到 `/workspace/` 下再 execute（容器只看得到那里）。\n"
        "6. 改本机文件属于敏感操作（默认需要用户审批）；不要碰系统目录（如 C:/Windows），"
        "不要把密钥文件的内容复制到别处。\n"
    )
    return "".join(lines)


def build_system_prompt(locale: str | None = None, mode: str = "sandbox") -> str:
    """返回主 Agent 的系统提示词（框架规则；偏好由 memory 通道注入）。

    本机磁盘挂载说明是**按当前配置动态生成**的（列出真实挂载点），
    没开启 `HARNESS_MACHINE_ACCESS` 时不会多出这一段。

    末尾追加「工具调用意图标签（`_intent`）」段落，让模型每次调工具时
    都吐一句给用户看的说明（任务看板的"当前步骤"靠它）。`INTENT_LABEL_ENABLED=false`
    时这一段不会出现，提示词与逐字节相同。

    Args:
        locale: 意图段落的语言（`zh*` / `en*`）。留空时用配置 `INTENT_LOCALE`；
            段落里另外要求"用用户提问的语言写"，所以会话内切语言也能跟随。
        mode: 权限模式（`app/agent/access.py` 的 `MODE_SANDBOX` / `MODE_FULL`）。
            - `sandbox`：`_FS_TOOLS_SANDBOX` + `machine_mount_prompt_section()`
            - `full`   ：`_FS_TOOLS_FULL`   + `full_access_prompt_section()`
            两套工具说明**互斥**，不会同时出现在提示词里。
    """
    from app.agent import access
    from app.agent.intent import render_intent_prompt_section
    from app.core.config import settings

    resolved = access.normalize_mode(mode)
    if resolved == access.MODE_FULL:
        prompt = (
            _PROMPT_HEAD
            + _FS_TOOLS_FULL
            + _PROMPT_TAIL
            + full_access_prompt_section()
        )
    else:
        prompt = BASE_SYSTEM_PROMPT + machine_mount_prompt_section()
    if getattr(settings, "intent_label_enabled", True):
        prompt += render_intent_prompt_section(locale or getattr(settings, "intent_locale", "zh"))
    return prompt


def full_access_prompt_section() -> str:
    """「完全访问模式」的运行环境段落。

    与 `machine_mount_prompt_section()` 是**互为替代**的两段（同一位置、不同模式）：
    后者讲"默认工作环境是本机磁盘，但 execute 仍进沙箱"，这一段讲"连 execute 都在本机"。

    必须交代清楚的四件事（少一件模型就会跑去试）：
    1. `execute` 不做任何隔离 —— 不要再假设"命令在容器里、看不到本机"；
    2. 文件工具用虚拟路径 `/mnt/<盘>/...`，execute 用 Windows 真实路径 —— **两套别混**；
    3. 工作目录就是工作区目录，相对路径直接可用；
    4. 没有审批兜底，破坏性命令要自己避免（这是用户显式选择的模式，但模型不该趁人之危）。
    """
    from app.agent.opensandbox_backend import machine_mounts

    mounts = machine_mounts()

    lines = [
        "\n## 运行环境：完全访问模式（命令直接在本机执行）\n",
        "用户已把权限模式切到**完全访问**：`execute` 的 shell 命令**直接在这台电脑上运行**，"
        "不存在沙箱容器、不存在文件同步、也没有审批弹窗。"
        "换句话说，你说的每一句命令都会立刻在这台机器上生效。\n\n",
    ]
    if mounts:
        lines.append("### 路径对照（用户说的 → 你调用工具时写的）\n")
        for name, path in mounts:
            lines.append(
                f"- 文件工具：`/mnt/{name}/` = `{path}`\n"
                f"- execute ：`{path}`（真实路径，如 `{path}a\\b.py`）\n"
            )
        lines.append(
            "- 用户说「D 盘 / 我的项目 / 桌面」时：文件工具写 `/mnt/<小写盘符>/...`，"
            "execute 写真盘符 `D:\\...`；反斜杠在文件工具里一律写成 `/`。\n"
            "- 没有明确路径、或者说「帮我做个报告」时，用 `/workspace/...`"
            "（工作区；交付物落这里，用户能一键下载）。\n\n"
        )
    else:
        lines.append(
            "### 路径说明\n"
            "- 文件工具目前**只能访问工作区** `/workspace/...`"
            "（本机磁盘挂载未开启，见 `HARNESS_MACHINE_ACCESS`）。\n"
            "- 需要读写工作区之外的文件时，用 `execute` 走本机 shell（那里可以用任意真实路径）。\n\n"
        )
    lines.append(
        "### 硬性要求\n"
        "1. 用户提到本机文件，文件工具用 `/mnt/...`、execute 用真实盘符路径，**一步到位**，"
        "不要在两种写法之间来回试。\n"
        "2. **不要**再假设命令在容器里：容器专用的说法（如「沙箱里看不到 /mnt」）在此模式下不成立。\n"
        "3. 破坏性命令（删除、覆盖、`git reset --hard`、安装全局包）先确认影响范围；"
        "本模式下没有审批兜底，你自己就是最后一道防线。\n"
        "4. 用 `glob` / `grep` 搜索**必须带 `path=`**（如 `path=/mnt/d/proj`）；"
        "不带 path 会扫全盘，又慢又容易被 5 秒超时砍成空结果。\n"
        "5. 不要把密钥文件（`.env`、私钥、凭据）的内容复制到别处或写进回复里。\n"
    )
    return "".join(lines)


def memory_sources(user_id: str | None = None) -> list[str]:
    """传给 create_deep_agent(memory=...) 的源列表；文件不存在则返回空。

    注意：`MemoryMiddleware` 只把**存在**的文件注入提示词；文件缺失时它会静默跳过
    （`file_not_found`），所以这里也顺手保证文件存在，语义更明确。

    `user_id` 决定这是谁的长期记忆（见 `memory_file_path`）。
    这里返回的仍是**虚拟路径** `/workspace/Agent.md` —— 它由（按用户构建的）
    backend 解析到该用户的工作区，所以同一份提示词配置对每个用户都成立。
    """
    path = ensure_memory_file(user_id)
    return [AGENT_MEMORY_PATH] if path.exists() else []
