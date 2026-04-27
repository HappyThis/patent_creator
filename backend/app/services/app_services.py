from __future__ import annotations

from ..agents.runtime.openai_compat import OpenAICompatibleClient
from ..core import Settings
from ..runtime import ContextManager, ExecutorEngine
from ..storage.workspace_store import WorkspaceStore
from .chat import ChatService
from .event_bus import SessionEventBus


class AppServices:
    def __init__(self, settings: Settings, llm_client: OpenAICompatibleClient | None = None) -> None:
        self.settings = settings
        self.store = WorkspaceStore(settings.data_dir, settings.git_user_name, settings.git_user_email)
        self.context_manager = ContextManager(self.store)
        self.llm_client = llm_client or OpenAICompatibleClient(settings)
        self.executor = ExecutorEngine(self.store, self.context_manager, self.llm_client)
        self.bus = SessionEventBus()
        self.chat = ChatService(
            self.store,
            self.context_manager,
            self.executor,
            self.bus,
            settings,
            self.llm_client,
        )
