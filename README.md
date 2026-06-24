# mcp-notebooklm

MCP server that exposes [Google NotebookLM](https://notebooklm.google.com/)
notebooks as tools for AI assistants (opencode).

Thin wrapper over [notebooklm-py](https://github.com/teng-lin/notebooklm-py)
using [`FastMCP`](https://github.com/modelcontextprotocol/python-sdk).

## What it implements

A stdio MCP server with three tools:

| Tool                                  | Purpose                                                      |
| ------------------------------------- | ------------------------------------------------------------ |
| `list_notebooks()`                    | List every notebook with `id`, `title`, `source_count`.      |
| `find_notebook(title)`                | Case-insensitive partial match over notebook titles.         |
| `ask_notebook(notebook_id, question)` | Ask a question; returns the grounded answer from NotebookLM. |

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

## 2. Authenticate with NotebookLM

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

## 3. Register with opencode

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

Restart opencode. The three tools (`list_notebooks`, `find_notebook`,
`ask_notebook`) appear as `notebooklm__*` and are available immediately.

> The `timeout` (120s) covers the worst-case ask path: 10s queue + 60s ask +
> overhead. Raise it if you see transport resets on slow networks.

## 4. Use it

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

## Project layout

```text
src/mcp_notebooklm/
├── __init__.py
├── main.py        # entry point → server.main()
├── server.py      # FastMCP tool definitions
└── service.py     # NotebookLMClient wrapper + concurrency / timeout guards
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
  bug.
- Only `chat.ask` is exposed. Source management, artifact generation, etc. are
  not wired into the MCP surface yet.
- The server is stdio-only. For HTTP, look at the upstream `notebooklm-py` REST
  server.
