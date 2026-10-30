"""Refresh user memory at each new run, including existing checkpoints."""
from langchain.agents.middleware.types import AgentMiddleware
from deepagents.middleware.memory import MemoryState
from app.core.config import settings
from app.agent.memory_storage import DETAIL_DIRECTORY

class WorkspaceMemoryRepairMiddleware(AgentMiddleware):
    state_schema = MemoryState

    def __init__(self, backend, sources):
        self.backend = backend
        self.sources = list(sources)

    async def abefore_agent(self, state, runtime):
        contents = {}
        if not self.sources:
            return {"memory_contents": {}} if state.get("memory_contents") else None
        responses = await self.backend.adownload_files(self.sources)
        for source, response in zip(self.sources, responses, strict=True):
            if response.error == "file_not_found":
                contents.pop(source, None)
                continue
            if response.error:
                raise ValueError(f"Failed to load user memory: {response.error}")
            if response.content is not None:
                content = response.content.decode("utf-8")
                limit = settings.memory_core_max_chars
                directory = source.rsplit("/", 1)[0] + "/" + DETAIL_DIRECTORY
                if len(content) > limit:
                    content = content[:limit] + f"\n\n[常驻记忆仅加载前 {limit} 字；完整内容仍保存在 {source}，请用 read_file 分页回查。详细经验请移入 {directory}/。]"
                listing = await self.backend.als(directory)
                if not listing.error:
                    lines = [f"\n\n## 按需记忆索引\n以下文件未加载正文；需要时 read_file，更多文件可 ls {directory}。\n"]
                    used = len(lines[0])
                    for entry in sorted(listing.entries, key=lambda item: item["path"]):
                        if entry.get("is_dir") or not entry["path"].endswith(".md"):
                            continue
                        line = f"- {entry['path']}（更新：{entry.get('modified_at', '未知')}）\n"
                        if used + len(line) > settings.memory_index_max_chars:
                            break
                        lines.append(line)
                        used += len(line)
                    if len(lines) > 1:
                        content += "".join(lines)
                contents[source] = content
        if contents != state.get("memory_contents"):
            return {"memory_contents": contents}
        return None
