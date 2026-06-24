# mcp-notebooklm

MCP server that exposes [Google NotebookLM](https://notebooklm.google.com/)
notebooks as tools for AI assistants (opencode).

Thin wrapper over [notebooklm-py](https://github.com/teng-lin/notebooklm-py)
using [`FastMCP`](https://github.com/modelcontextprotocol/python-sdk).

## What it implements

A stdio MCP server with four tools:

| Tool                                             | Purpose                                                                    |
| ------------------------------------------------ | -------------------------------------------------------------------------- |
| `list_notebooks()`                               | List every notebook with `id`, `title`, `source_count`.                    |
| `find_notebook(title)`                           | Case-insensitive partial match over notebook titles.                       |
| `ask_notebook(notebook_id, question)`            | Ask a question; returns the grounded answer from NotebookLM.               |
| `generate_quiz(notebook_id, num_questions, ...)` | Generate quiz JSON with multiple_choice / true_false questions in batches. |

The service (`src/mcp_notebooklm/service.py`) wraps the async `NotebookLMClient`
and applies two safety rails so the host LLM never hangs:

- **Concurrency limit** — one in-flight query at a time
  (`asyncio.Semaphore(1)`), with a 10s queue timeout. If another query is
  running, the tool returns
  `[RETRY_NEEDED] NotebookLM is busy... call again in ~60 seconds.`
- **Execution timeout** — 60s hard cap on the underlying `chat.ask()`. Timeouts
  and transport failures surface as `[RETRY_NEEDED]` / `[ERROR]` prefixes so the
  LLM can react instead of looping.

Authentication state is persisted to `data/auth.json` and reloaded on every
request via `NotebookLMClient.from_storage()`.

## Prerequisites

- Python ≥ 3.12
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pipx`
- A Google account with NotebookLM access

## 1. Install

From the project root:

```bash
uv sync
```

This installs the project (including the `notebooklm-py[browser]` extra, which
pulls Playwright) and exposes the `mcp-notebooklm` console script.

## 2. Install Playwright browser (one-time)

notebooklm-py[browser] installs the Playwright Python package, but the Chromium
browser binary must be downloaded separately.

uv run playwright install chromium

This is a one-time step (~170 MB) and must be done before the first login.

## 3. Authenticate with NotebookLM

The first run downloads Chromium (~170 MB) and opens a Google sign-in window.
Auth state is written to `data/auth.json` and reused on subsequent calls.

```bash
# one-time login (interactive — finishes in the browser)
uv run --project notebooklm login --storage-path ./data/auth.json

# verify the session
uv run --project notebooklm auth check --test
```

The login command is provided by the upstream `notebooklm-py` CLI; see
[its README](https://github.com/teng-lin/notebooklm-py) for browser options
(`--browser msedge`, `--browser-cookies chrome`, multi-account `--profile`,
etc.).

> `data/auth.json` is git-ignored. Back it up somewhere safe — it is the only
> thing standing between you and a fresh login.

To refresh cookies silently (cron / launchd / systemd):

```bash
notebooklm auth refresh --quiet
```

## 4. Register with opencode

Add the server to `~/.config/opencode/opencode.json`:

```jsonc
{
  "mcp": {
    "notebooklm": {
      "command": [
        "uv",
        "run",
        "--project",
        "<your-path>/mcp-notebooklm",
        "mcp-notebooklm",
      ],
      "timeout": 120000,
      "type": "local",
    },
  },
}
```

Restart opencode. The four tools (`list_notebooks`, `find_notebook`,
`ask_notebook`, `generate_quiz`) appear as `notebooklm__*` and are available
immediately.

> The `timeout` (120s) covers the worst-case ask path: 10s queue + 60s ask +
> overhead. Raise it if you see transport resets on slow networks.

## 5. Use it

From inside opencode (or any MCP host):

```text
list all my NotebookLM notebooks
```

```text
find the notebook about <topic>
```

```text
ask notebook <notebook_id>: <question grounded in that notebook's sources>
```

Typical flow the LLM will follow:

1. `list_notebooks()` → choose the right `notebook_id`.
2. `ask_notebook(id, question)` → get a cited answer.
3. If the response starts with `[RETRY_NEEDED]`, call the tool again.

### Quiz generation

Generate structured quizzes that bypass NotebookLM's ~20 question limit by
batching requests:

```text
generate_quiz(
    notebook_id = "abc123",
    num_questions = 50,
    topic = "sorting algorithms",
    difficulty = "mixed",
    output_path = "~/quizzes/algorithms.json",
    cumulative = true,
    language = "es"
)
```

**Parameters:**

| Param           | Required | Default         | Description                                                       |
| --------------- | -------- | --------------- | ----------------------------------------------------------------- |
| `notebook_id`   | yes      | —               | Notebook to query                                                 |
| `num_questions` | yes      | —               | Total questions to generate                                       |
| `topic`         | no       | `"all sources"` | Specific topic or full notebook                                   |
| `difficulty`    | no       | `"mixed"`       | `easy` / `medium` / `hard` / `mixed`                              |
| `output_path`   | no       | —               | Save JSON to this path (creates directories if needed)            |
| `cumulative`    | no       | `false`         | If `true` and file exists, merge new questions with existing ones |
| `language`      | no       | `"es"`          | Language for questions, options, and explanations                 |

**How batching works:**

Questions are generated in batches of 15. For 50 questions, the tool makes 4
calls to NotebookLM (15 + 15 + 15 + 5), parses each response, and merges them
into a single JSON. If a batch fails (timeout, parse error), it's skipped and
`failed_batches` in metadata tells you how many were lost.

**Output format:**

```json
{
  "metadata": {
    "notebook_id": "abc123",
    "notebook_title": "Algorithms",
    "generated_at": "2026-06-24T10:30:00Z",
    "topic": "sorting algorithms",
    "difficulty": "mixed",
    "total_questions": 50,
    "failed_batches": 0
  },
  "questions": [
    {
      "id": 1,
      "type": "multiple_choice",
      "question": "What is the average time complexity of quicksort?",
      "options": [
        {
          "text": "O(n)",
          "rationale": "Incorrect. Linear time only applies to specific cases like searching in unsorted arrays."
        },
        {
          "text": "O(n log n)",
          "rationale": "Correct. Quicksort averages O(n log n) with good pivot selection and balanced partitions."
        },
        {
          "text": "O(n²)",
          "rationale": "Incorrect. This is the worst-case complexity when the pivot selection is poor (e.g., already sorted array with first/last element as pivot)."
        },
        {
          "text": "O(log n)",
          "rationale": "Incorrect. Logarithmic time applies to operations like binary search, not full sorting algorithms."
        }
      ],
      "correct_answer": 1
    },
    {
      "id": 2,
      "type": "true_false",
      "question": "Mergesort is a stable sorting algorithm.",
      "correct_answer": true,
      "explanation": "Mergesort preserves the relative order of equal elements, making it stable."
    }
  ]
}
```

**Cumulative mode:**

When `cumulative = true` and `output_path` exists, new questions are appended to
the existing array and IDs are renumbered sequentially. This lets you build up a
question bank over multiple calls.

## Project layout

```text
src/mcp_notebooklm/
├── __init__.py
├── main.py        # entry point → server.main()
├── server.py      # FastMCP tool definitions (list, find, ask, generate_quiz)
└── service.py     # NotebookLMClient wrapper + concurrency / timeout guards + quiz generation
data/
└── auth.json      # notebooklm-py session storage (git-ignored)
pyproject.toml     # deps, entry point: mcp-notebooklm
```

## Development

```bash
uv run ruff check src/        # lint
uv run flake8 src/           # style
```

## Notes & limits

- The upstream library uses **undocumented Google APIs** — endpoints can break
  without notice.
- Heavy usage is rate-limited; the 1-concurrent semaphore is intentional, not a
  bug. Quiz generation with many questions will take time due to sequential
  batches.
- `chat.ask` is the only endpoint used. Source management, artifact generation,
  etc. are not wired into the MCP surface.
- The server is stdio-only. For HTTP, look at the upstream `notebooklm-py` REST
  server.
