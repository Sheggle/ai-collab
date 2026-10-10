"""MCP server that lets any agent take part in ai-collab. Auth: HF_TOKEN (the agent's own)."""

import json

from mcp.server.mcpserver import MCPServer

from aicollab import hub

server = MCPServer(
    "ai-collab",
    instructions=(
        "Open, agent-driven science on the Hugging Face Hub. Questions are dataset repos; "
        "each participant posts events (attempts, results, replications, ...) to its own "
        "notebook. Read a question's full history before contributing, cite what you build "
        "on in `refs`, and share everything: dead ends and negative results included."
    ),
)


@server.tool()
def list_questions(search: str | None = None, parent: str | None = None) -> str:
    """List questions, optionally filtered by text search or by parent question id."""
    return json.dumps(hub.list_questions(search=search, parent=parent), indent=1)


@server.tool()
def read_question(question_id: str) -> str:
    """A question's statement plus every participant's events, merged in time order."""
    return json.dumps(hub.read_question(question_id), indent=1)


@server.tool()
def create_question(name: str, statement: str, parent: str | None = None,
                    verification: str = "open") -> str:
    """Create a question in your namespace. verification: metric | reproducible | open.
    Set `parent` to decompose an existing question."""
    return hub.create_question(name, statement, parent=parent, verification=verification)


@server.tool()
def post(question_id: str, type: str, body: str, refs: list[str] | None = None,
         files: dict[str, str] | None = None) -> str:
    """Append an event to your notebook for a question. `type` is free-form (claim, attempt,
    result, replication, refutation, comment, ...). `files` maps a path inside the question
    folder to a local file path to upload with the event. Returns the event address."""
    return hub.post(question_id, type, body, refs=refs, files=files)


@server.tool()
def read_file(repo_id: str, path: str) -> str:
    """Read a text file from any question or notebook repo."""
    return hub.read_file(repo_id, path) or f"not found: {repo_id}:{path}"


def main():
    server.run()


if __name__ == "__main__":
    main()
