"""Workspace-scoped durable memory entries with provenance and bounded core."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from langchain_core.tools import tool, ToolException
from pydantic import Field

from app.agent.memory_storage import DETAIL_DIRECTORY, MEMORY_WRITE_LOCK, MemoryConflict, memory_path, read_snapshot, write_snapshot
from app.core.config import settings


def build_memory_tools(workspace: Path):
    @tool
    def remember(key: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")],
                 title: Annotated[str, Field(min_length=1, max_length=120)],
                 content: Annotated[str, Field(min_length=1, max_length=12000)],
                 source: Annotated[str, Field(min_length=1, max_length=500)],
                 tier: Literal["core", "detail"] = "detail",
                 expected_revision: Annotated[str, Field(min_length=1, max_length=64)] = "missing") -> str:
        """保存用户确认的长期偏好或项目经验，附消息ID/证据路径 source；临时进度使用 update_task_state。

        detail 写入独立 Markdown 文件，按需读取；core 写入常驻 Agent.md，容量有限。
        首次创建 expected_revision=missing；修改既有文件须传此前返回的 revision。
        冲突返回当前 revision，必须先读现有内容再合并，避免覆盖用户修改。不要保存凭据或未确认推测。
        """
        relative = "Agent.md" if tier == "core" else f"{DETAIL_DIRECTORY}/{key}.md"
        try:
            with MEMORY_WRITE_LOCK:
                path = memory_path(workspace, relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                prior = read_snapshot(path)
                if prior["revision"] != expected_revision:
                    raise MemoryConflict("记忆版本冲突")
                stamp = datetime.now(timezone.utc).isoformat()
                label = title.replace("\n", " ").replace("\r", " ")
                entry = f"## {label}\n\n- 来源：{source}\n- 更新时间：{stamp}\n- 范围：当前工作区\n\n{content.strip()}\n"
                if tier == "core":
                    start, end = f"<!-- memory-entry:{key} -->", f"<!-- /memory-entry:{key} -->"
                    block = f"{start}\n{entry}{end}\n"
                    pattern = re.escape(start) + r".*?" + re.escape(end) + r"\n?"
                    value = re.sub(pattern, lambda _: block, prior["content"], flags=re.DOTALL) if start in prior["content"] else prior["content"].rstrip() + "\n\n" + block
                    if len(value) > settings.memory_core_max_chars:
                        raise ToolException(json.dumps({"error": "core_full", "limit": settings.memory_core_max_chars,
                                           "hint": "请将详细经验保存为 detail，核心记忆只保留简短偏好和约定"}, ensure_ascii=False))
                else:
                    value = entry
                saved = write_snapshot(path, value, expected_revision)
                return json.dumps({"saved": True, "path": f"/workspace/{relative}", "revision": saved["revision"], "tier": tier}, ensure_ascii=False)
        except MemoryConflict:
            latest = read_snapshot(memory_path(workspace, relative))
            raise ToolException(json.dumps({"error": "revision_conflict", "revision": latest["revision"],
                               "path": f"/workspace/{relative}", "hint": "先用 read_file 读取现有内容并合并，再用此 revision 重试"}, ensure_ascii=False))
        except (ValueError, OSError) as exc:
            raise ToolException(json.dumps({"error": str(exc)}, ensure_ascii=False)) from exc

    remember.handle_tool_error = True
    return [remember]
