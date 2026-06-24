import asyncio
from pathlib import Path

from notebooklm import NotebookLMClient
from notebooklm.types import AskResult

AUTH_FILE = Path(__file__).parent.parent.parent / "data" / "auth.json"
QUEUE_TIMEOUT = 10  # seconds
EXECUTION_TIMEOUT = 60  # seconds


class NotebookLMService:
    """Thin wrapper around notebooklm-py for the MCP server."""

    def __init__(self):
        self._client: NotebookLMClient | None = None
        self._sem = asyncio.Semaphore(1)

    async def connect(self):
        if self._client is None:
            self._client = await NotebookLMClient.from_storage(
                str(AUTH_FILE)
            ).__aenter__()

    async def close(self):
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None

    @property
    def client(self) -> NotebookLMClient:
        if self._client is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._client

    async def _ask_safe(self, notebook_id: str, question: str) -> AskResult:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "NotebookLM is busy processing another query. "
                "Please call ask_notebook again in ~60 seconds."
            )
        try:
            return await asyncio.wait_for(
                self.client.chat.ask(notebook_id, question),
                timeout=EXECUTION_TIMEOUT,
            )
        finally:
            self._sem.release()

    async def list_notebooks(self) -> list[dict]:
        await self.connect()
        notebooks = await self.client.notebooks.list()
        return [
            {
                "id": nb.id,
                "title": nb.title,
                "sources_count": nb.sources_count,
            }
            for nb in notebooks
        ]

    async def get_notebook(self, notebook_id: str) -> dict:
        await self.connect()
        notebook = await self.client.notebooks.get(notebook_id)
        return {
            "id": notebook.id,
            "title": notebook.title,
            "sources_count": notebook.sources_count,
        }

    async def find_notebook_by_title(self, title: str) -> list[dict]:
        await self.connect()
        notebooks = await self.client.notebooks.list()
        return [
            {
                "id": nb.id,
                "title": nb.title,
                "sources_count": nb.sources_count,
            }
            for nb in notebooks
            if title.lower() in nb.title.lower()
        ]

    async def ask(self, notebook_id: str, question: str) -> AskResult:
        await self.connect()
        return await self._ask_safe(notebook_id, question)
