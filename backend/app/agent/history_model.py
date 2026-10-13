"""Non-executable model for reconstructing persisted graph state offline."""
from langchain_core.language_models.chat_models import BaseChatModel


class HistoryModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "history-reader"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("The history graph cannot execute model calls")
