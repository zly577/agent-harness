"""Refresh workspace memory at each new run, including existing checkpoints."""
from langchain.agents.middleware.types import AgentMiddleware
from deepagents.middleware.memory import MemoryState

class WorkspaceMemoryRepairMiddleware(AgentMiddleware):
    state_schema = MemoryState

    def __init__(self, backend, sources):
        self.backend = backend
        self.sources = list(sources)

    async def abefore_agent(self, state, runtime):
        if "memory_contents" not in state:
            return None  # The normal MemoryMiddleware handles the first load.
        contents = dict(state.get("memory_contents") or {})
        if not self.sources:
            return None
        responses = await self.backend.adownload_files(self.sources)
        for source, response in zip(self.sources, responses, strict=True):
            if response.error == "file_not_found":
                contents.pop(source, None)
                continue
            if response.error:
                raise ValueError(f"Failed to load workspace memory: {response.error}")
            if response.content is not None:
                contents[source] = response.content.decode("utf-8")
        if contents != state.get("memory_contents"):
            return {"memory_contents": contents}
        return None
