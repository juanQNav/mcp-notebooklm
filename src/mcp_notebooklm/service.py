import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from notebooklm import NotebookLMClient
from notebooklm.types import AskResult

AUTH_FILE = Path(__file__).parent.parent.parent / "data" / "auth.json"
QUEUE_TIMEOUT = 600  # seconds
EXECUTION_TIMEOUT = 300  # seconds
QUIZ_BATCH_SIZE = 15

QUIZ_PROMPT_TEMPLATE = """\
Generate exactly {count} quiz questions about "{topic}" \
based on this notebook's sources.

Requirements:
- Difficulty: {difficulty}
- Question types: roughly half multiple_choice (4 options each) \
and half true_false
- Language for questions, options, and explanations: {language}
- Question IDs must go from {start_id} to {end_id}
- For multiple_choice: each option MUST include a rationale explaining \
why it is correct or incorrect
- For true_false: include an explanation for the correct answer
- Questions should test understanding, not just memorization

Return ONLY a valid JSON object. No markdown code fences, \
no text outside the JSON. Use this exact structure:
{{
  "questions": [
    {{
      "id": {start_id},
      "type": "multiple_choice",
      "question": "Question text here?",
      "options": [
        {{"text": "Option A", "rationale": "Why this is correct/incorrect"}},
        {{"text": "Option B", "rationale": "Why this is correct/incorrect"}},
        {{"text": "Option C", "rationale": "Why this is correct/incorrect"}},
        {{"text": "Option D", "rationale": "Why this is correct/incorrect"}}
      ],
      "correct_answer": 0
    }},
    {{
      "id": {next_id},
      "type": "true_false",
      "question": "Statement here.",
      "correct_answer": true,
      "explanation": "Why this is true/false."
    }}
  ]
}}

CRITICAL: You MUST generate exactly {count} questions. \
Not fewer, not more."""


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

    # ── Quiz generation ──────────────────────────────────────────────

    def _build_quiz_prompt(
        self,
        count: int,
        start_id: int,
        topic: str,
        difficulty: str,
        language: str,
    ) -> str:
        return QUIZ_PROMPT_TEMPLATE.format(
            count=count,
            start_id=start_id,
            end_id=start_id + count - 1,
            next_id=start_id + 1,
            topic=topic,
            difficulty=difficulty,
            language=language,
        )

    def _parse_quiz_response(self, raw: str) -> list[dict]:
        """Extract and validate quiz questions from an LLM response."""
        text = raw.strip()

        # Strip markdown code fences
        fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()

        # Find the largest JSON blob (object or array)
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        bracket = re.search(r"\[.*\]", text, re.DOTALL)
        candidates = sorted(
            [m.group(0) for m in (brace, bracket) if m],
            key=len,
            reverse=True,
        )
        if not candidates:
            raise ValueError("No JSON found in response")

        data = None
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue

        if data is None:
            raise ValueError("Could not parse JSON from response")

        # Accept {"questions": [...]} or bare [...]
        if isinstance(data, list):
            questions = data
        elif isinstance(data, dict):
            questions = data.get("questions", [])
        else:
            raise ValueError("Unexpected JSON structure")

        validated = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            if "type" not in q or "question" not in q:
                continue
            qtype = q["type"]
            if qtype == "multiple_choice":
                if "correct_answer" not in q:
                    continue
                opts = q.get("options")
                if not isinstance(opts, list) or len(opts) < 2:
                    continue
                # Validate each option has text and rationale
                for opt in opts:
                    if not isinstance(opt, dict):
                        continue
                    if "text" not in opt or "rationale" not in opt:
                        continue
            elif qtype == "true_false":
                if "correct_answer" not in q:
                    continue
            else:
                continue  # unknown type
            validated.append(q)

        if not validated:
            raise ValueError("No valid questions found in response")
        return validated

    async def generate_quiz(
        self,
        notebook_id: str,
        num_questions: int,
        topic: str = "all sources",
        difficulty: str = "mixed",
        output_path: str | None = None,
        cumulative: bool = False,
        language: str = "es",
    ) -> dict:
        """Generate quiz questions from a notebook in batches."""
        if num_questions < 1:
            raise ValueError("num_questions must be >= 1")

        await self.connect()
        notebook_info = await self.get_notebook(notebook_id)

        all_questions: list[dict] = []
        start_id = 1
        remaining = num_questions
        failed_batches = 0

        while remaining > 0:
            batch_size = min(remaining, QUIZ_BATCH_SIZE)
            prompt = self._build_quiz_prompt(
                batch_size, start_id, topic, difficulty, language
            )
            try:
                result = await self._ask_safe(notebook_id, prompt)
                batch = self._parse_quiz_response(result.answer)
                all_questions.extend(batch)
            except Exception:
                failed_batches += 1
            start_id += batch_size
            remaining -= batch_size

        # Renumber IDs sequentially
        for idx, q in enumerate(all_questions, 1):
            q["id"] = idx

        quiz: dict = {
            "metadata": {
                "notebook_id": notebook_id,
                "notebook_title": notebook_info["title"],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "topic": topic,
                "difficulty": difficulty,
                "total_questions": len(all_questions),
                "failed_batches": failed_batches,
            },
            "questions": all_questions,
        }

        # Optional: write / merge to disk
        if output_path:
            path = Path(output_path)

            if cumulative and path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    prev = existing.get("questions", [])
                    offset = len(prev)
                    for idx, q in enumerate(all_questions, offset + 1):
                        q["id"] = idx
                    merged = prev + all_questions
                    quiz["questions"] = merged
                    quiz["metadata"]["total_questions"] = len(merged)
                    quiz["metadata"]["cumulative"] = True
                except (json.JSONDecodeError, KeyError):
                    pass  # corrupt file → overwrite with fresh quiz

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(quiz, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        return quiz
