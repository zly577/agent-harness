"""Cheap, recoverable request-view reductions. Raw graph messages stay intact."""
import hashlib
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class ContextCleaner:
    def __init__(self, backend, session_id, tool_tokens):
        self.backend = backend
        self.scope = hashlib.sha256((session_id or "local").encode()).hexdigest()[:24]
        root = getattr(backend, "artifacts_root", "/").rstrip("/")
        self.root = f"{root}/context_results/{self.scope}"
        self.tool_tokens = tool_tokens

    async def _offload(self, message, reason):
        content = message.content
        digest = hashlib.sha256(content.encode()).hexdigest()
        path = f"{self.root}/{digest}.txt"
        try:
            responses = await self.backend.aupload_files([(path, content.encode("utf-8"))])
            if not responses or any(response.error for response in responses):
                return message, False
        except Exception:
            return message, False  # Never discard data whose recovery copy failed.
        preview = content[:240] + ("\n…\n" + content[-240:] if len(content) > 480 else "")
        receipt = f"[{reason}；完整原文：{path}。用 read_file 分页读取，需要细节时必须回查。]\n{preview}"
        return message.model_copy(update={"content": receipt}), True

    async def clean(self, messages, *, pressure, cutoff=0):
        result = list(messages)
        stats = {"offloaded_results": 0, "deduplicated_reads": 0}
        calls, seen, duplicates = {}, set(), set()
        for index in range(len(messages) - 1, cutoff - 1, -1):
            message = messages[index]
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    calls[call["id"]] = call
        if pressure:
            for index in range(len(messages) - 1, cutoff - 1, -1):
                message = messages[index]
                if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
                    continue
                call = calls.get(message.tool_call_id, {})
                if call.get("name") != "read_file" or message.status == "error":
                    continue
                # Display-only intent labels can differ between identical reads.
                args = {key: value for key, value in call.get("args", {}).items() if key != "_intent"}
                key = (json.dumps(args, sort_keys=True, ensure_ascii=False), message.content)
                if key in seen:
                    duplicates.add(index)
                seen.add(key)
        for index in range(cutoff, len(messages)):
            message = messages[index]
            # Media and structured tool results remain typed. FilesystemMiddleware
            # handles its supported media formats; never flatten binary into text.
            if not isinstance(message.content, str) or not message.content:
                continue
            if getattr(message, "additional_kwargs", {}).get("lc_source") in ("summarization", "workspace_memory"):
                continue
            large = len(message.content) > self.tool_tokens * 2
            old_tool = pressure and index < len(messages) - 6 and len(message.content) > 800
            if isinstance(message, ToolMessage) and (large or old_tool or index in duplicates):
                reason = "重复读取结果已去重" if index in duplicates else "工具结果已外置"
            elif isinstance(message, HumanMessage) and large:
                reason = "大型用户消息已外置，执行前须读取相关原文"
            else:
                continue
            result[index], saved = await self._offload(message, reason)
            if saved:
                stats["offloaded_results"] += 1
                stats["deduplicated_reads"] += int(index in duplicates)
        return result, stats
