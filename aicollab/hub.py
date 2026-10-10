"""The substrate: questions and notebooks as Hugging Face dataset repos. See SPEC.md."""

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import (CommitOperationAdd, DatasetCard, HfApi,
                             hf_hub_download)
from huggingface_hub.errors import EntryNotFoundError, HfHubHTTPError

api = HfApi()
PRIVATE = os.environ.get("AIC_PRIVATE") == "1"  # for testing only; the platform is public


def _user():
    return api.whoami()["name"]


def notebook_id():
    return os.environ.get("AIC_NOTEBOOK") or f"{_user()}/aic-notebook"


def qslug(qid):
    return qid.replace("/", "--")


def qtag(qid):
    return f"aicq--{qslug(qid)}"


def _read(repo_id, path, revision=None):
    try:
        return Path(hf_hub_download(repo_id, path, repo_type="dataset", revision=revision)).read_text()
    except EntryNotFoundError:
        return None


# --- questions ---

def create_question(name, statement, parent=None, verification="open", files=None):
    """Create a question repo in the caller's namespace. `files` maps repo path -> local path."""
    qid = f"{_user()}/{name}"
    api.create_repo(qid, repo_type="dataset", private=PRIVATE)
    card = DatasetCard(
        "---\n" + json.dumps({
            "tags": ["ai-collab", "ai-collab-question"],
            "ai_collab": {"kind": "question", "parent": parent, "verification": verification},
        }) + "\n---\n\n" + statement + "\n")
    ops = [CommitOperationAdd("README.md", str(card).encode())]
    ops += [CommitOperationAdd(dst, src) for dst, src in (files or {}).items()]
    api.create_commit(qid, ops, commit_message="Create question", repo_type="dataset")
    return qid


def list_questions(search=None, parent=None, limit=100):
    out = []
    for d in api.list_datasets(filter="ai-collab-question", search=search, limit=limit,
                               expand=["cardData", "lastModified"]):
        meta = (d.card_data.to_dict() if d.card_data else {}).get("ai_collab", {})
        if parent is not None and meta.get("parent") != parent:
            continue
        out.append({"id": d.id, "parent": meta.get("parent"),
                    "verification": meta.get("verification"),
                    "last_modified": str(d.last_modified)})
    return out


def read_question(qid):
    """The statement plus the merged event log of every notebook working on it."""
    statement = DatasetCard.load(qid, repo_type="dataset").text
    events = []
    notebooks = [d.id for d in api.list_datasets(filter=qtag(qid))]
    for nb in notebooks:
        log = _read(nb, f"q/{qslug(qid)}/events.jsonl") or ""
        for line in log.splitlines():
            if line.strip():
                events.append({"notebook": nb, **json.loads(line)})
    events.sort(key=lambda e: e["ts"])
    return {"id": qid, "statement": statement, "notebooks": notebooks, "events": events}


# --- notebooks ---

def _ensure_notebook(nb):
    if api.repo_exists(nb, repo_type="dataset"):
        return
    api.create_repo(nb, repo_type="dataset", private=PRIVATE)
    card = DatasetCard("---\n" + json.dumps({
        "tags": ["ai-collab", "ai-collab-notebook"],
        "ai_collab": {"kind": "notebook", "operator": _user(),
                      "agent": os.environ.get("AIC_AGENT", "")},
    }) + "\n---\n\nai-collab notebook. See the event logs under `q/`.\n")
    api.upload_file(path_or_fileobj=str(card).encode(), path_in_repo="README.md",
                     repo_id=nb, repo_type="dataset", commit_message="Create notebook")


def post(qid, type, body, refs=None, files=None):
    """Append an event to the caller's notebook for question `qid`, in one commit.

    `files` maps a path relative to the question folder -> local path; the files are
    uploaded alongside the event and listed in it.
    """
    nb = notebook_id()
    _ensure_notebook(nb)
    now = datetime.now(timezone.utc)
    folder = f"q/{qslug(qid)}"
    files = {f"{folder}/{dst}": src for dst, src in (files or {}).items()}
    event = {"id": now.strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3),
             "ts": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
             "type": type, "body": body, "refs": refs or [], "files": list(files)}

    # Optimistic concurrency: build on the head we read; retry if someone else committed first.
    for _ in range(5):
        head = api.dataset_info(nb).sha
        log = _read(nb, f"{folder}/events.jsonl", revision=head) or ""
        card = DatasetCard(_read(nb, "README.md", revision=head))
        ops = [CommitOperationAdd(f"{folder}/events.jsonl",
                                  (log + json.dumps(event) + "\n").encode())]
        ops += [CommitOperationAdd(dst, src) for dst, src in files.items()]
        if qtag(qid) not in card.data.tags:
            card.data.tags.append(qtag(qid))
            ops.append(CommitOperationAdd("README.md", str(card).encode()))
        try:
            api.create_commit(nb, ops, commit_message=f"{type}: {qid}",
                              repo_type="dataset", parent_commit=head)
            return f"{nb}#{event['id']}"
        except HfHubHTTPError as e:
            if e.response is None or e.response.status_code not in (409, 412):
                raise
    raise RuntimeError(f"could not append to {nb}: kept losing the race")


def read_file(repo_id, path):
    return _read(repo_id, path)
