from mcp.server import FastMCP

from mcp_notebooklm.service import NotebookLMService

mcp = FastMCP("notebooklm")
service = NotebookLMService()


@mcp.tool()
async def list_notebooks() -> list[dict]:
    """
    List all available NotebookLM notebooks with their IDs,
    titles, and source counts.
    """
    return await service.list_notebooks()


@mcp.tool()
async def find_notebook(title: str) -> list[dict]:
    """
    Find NotebookLM notebooks by title (partial match, case-insensitive).

    Args:
        title: The title or partial title to search for.
    """
    return await service.find_notebook_by_title(title)


@mcp.tool()
async def ask_notebook(notebook_id: str, question: str) -> str:
    """
    Ask a question to a specific NotebookLM notebook and get an AI answer
    based on its sources.

    Args:
        notebook_id: The ID of the notebook to query
        (use list_notebooks to find IDs).
        question: The question to ask the notebook.
    """
    try:
        response = await service.ask(notebook_id, question)
        return response.answer
    except RuntimeError as e:
        # LLM gets a clear retry signal instead of a cryptic timeout
        return f"[RETRY_NEEDED] {e}"
    except Exception as e:
        return f"[ERROR] Could not query notebook {e}"


@mcp.tool()
async def generate_quiz(
    notebook_id: str,
    num_questions: int,
    topic: str = "all sources",
    difficulty: str = "mixed",
    output_path: str | None = None,
    cumulative: bool = False,
    language: str = "es",
) -> dict:
    """
    Generate a quiz (JSON) from a NotebookLM notebook's sources.

    Generates questions in batches to bypass NotebookLM's ~20 question
    limit. Supports multiple_choice and true_false question types.

    Args:
        notebook_id: The notebook to generate questions from.
        num_questions: Total number of questions to generate.
        topic: Specific topic or "all sources" for everything.
        difficulty: easy, medium, hard, or mixed.
        output_path: Optional file path to save the JSON quiz.
        cumulative: If true and output_path exists, merge with existing.
        language: Language for questions (default: "es").
    """
    try:
        return await service.generate_quiz(
            notebook_id=notebook_id,
            num_questions=num_questions,
            topic=topic,
            difficulty=difficulty,
            output_path=output_path,
            cumulative=cumulative,
            language=language,
        )
    except RuntimeError as e:
        return {"error": f"[RETRY_NEEDED] {e}"}
    except Exception as e:
        return {"error": f"[ERROR] {e}"}


def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
