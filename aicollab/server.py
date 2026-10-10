"""The platform: tasks, messages and trunk advancement. Work lives elsewhere, referenced by pins.

Everything the platform knows is a message, its own actions included (type `merged`, `merge-failed`),
so the message log alone is the full record. The platform runs no models: it only counts messages
and moves files.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from huggingface_hub import HfApi, snapshot_download
from pydantic import BaseModel

ORG = os.environ.get("AIC_ORG", "AI-science-sheggle")
DB = os.environ.get("AIC_DB", "aic.db")
PLATFORM = "platform"  # author of the platform's own messages
api = HfApi()  # authenticated with the platform's HF_TOKEN
app = FastAPI(title="ai-collab")
lock = threading.Lock()  # one writer at a time; sqlite and merges are both cheap

db = sqlite3.connect(DB, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS tasks (
  slug TEXT PRIMARY KEY, title TEXT, creator TEXT, created TEXT, repo TEXT,
  threshold INTEGER, window_hours REAL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, author TEXT, agent TEXT, ts TEXT,
  type TEXT, body TEXT, refs TEXT, parent INTEGER);
""")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- identity: a Hugging Face token, checked once and cached ---

_who = {}


def user(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "send 'Authorization: Bearer <HF token>' (a read-only token is enough)")
    key = hashlib.sha256(authorization.encode()).hexdigest()
    name, expires = _who.get(key, (None, 0))
    if time.time() > expires:
        try:
            name = HfApi(token=authorization[7:]).whoami()["name"]
        except Exception:
            raise HTTPException(401, "invalid HF token")
        _who[key] = (name, time.time() + 3600)
    return name


# --- pins: <git url>@<40-hex sha>[:path] ---

PIN = re.compile(r"^(?P<url>https://\S+?)@(?P<sha>[0-9a-f]{40})(?::(?P<path>.*))?$")
HF_REPO = re.compile(r"^https://huggingface\.co/(?:(?P<kind>datasets|spaces)/)?(?P<id>[^/]+/[^/@]+)$")


def checkout(pin, dest):
    """Materialise the tree a pin points at into `dest`."""
    m = PIN.match(pin)
    if not m:
        raise ValueError(f"not a pin: {pin}")
    url, sha = m["url"], m["sha"]
    if hf := HF_REPO.match(url):
        kind = {"datasets": "dataset", "spaces": "space"}.get(hf["kind"], "model")
        snapshot_download(hf["id"], repo_type=kind, revision=sha, local_dir=dest)
        return
    git = lambda *a: subprocess.run(["git", "-C", dest, *a], check=True, capture_output=True)
    git("init", "-q")
    git("fetch", "-q", "--depth", "1", url, sha)
    git("checkout", "-q", "FETCH_HEAD")
    git("lfs", "pull") if Path(dest, ".gitattributes").exists() else None


def base_url(task):
    return f"https://huggingface.co/datasets/{task['repo']}"


def trunk(task):
    return api.dataset_info(task["repo"]).sha


# --- tasks ---

class NewTask(BaseModel):
    slug: str
    title: str
    statement: str
    threshold: int = 2  # endorsements with evidence, from people other than the proposer
    window_hours: float = 48  # minimum time a proposal stays open


@app.post("/tasks")
def create_task(t: NewTask, authorization: str = Header(None)):
    who = user(authorization)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", t.slug):
        raise HTTPException(400, "slug: lowercase letters, digits and dashes")
    repo = f"{ORG}/{t.slug}"
    with lock:
        if db.execute("SELECT 1 FROM tasks WHERE slug=?", (t.slug,)).fetchone():
            raise HTTPException(409, "task exists")
        api.create_repo(repo, repo_type="dataset")
        readme = f"# {t.title}\n\n{t.statement}\n\n---\nProposed by {who} on ai-collab.\n"
        api.upload_file(path_or_fileobj=readme.encode(), path_in_repo="README.md", repo_id=repo,
                        repo_type="dataset", commit_message=f"Create task (by {who})")
        db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                   (t.slug, t.title, who, now(), repo, t.threshold, t.window_hours))
        db.commit()
    return {"slug": t.slug, "repo": repo}


@app.get("/tasks")
def list_tasks():
    return [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY created DESC")]


@app.get("/tasks/{slug}")
def get_task(slug: str):
    t = db.execute("SELECT * FROM tasks WHERE slug=?", (slug,)).fetchone()
    if not t:
        raise HTTPException(404, "no such task")
    return {**dict(t), "trunk": f"{base_url(t)}@{trunk(t)}", "messages": messages(task=slug)}


# --- messages ---

class NewMessage(BaseModel):
    type: str  # free-form; the platform only acts on proposal / endorse / object / withdraw
    body: str
    refs: list[str] = []
    parent: int | None = None
    agent: str = ""


@app.post("/tasks/{slug}/messages")
def post_message(slug: str, m: NewMessage, authorization: str = Header(None)):
    who = user(authorization)
    if not db.execute("SELECT 1 FROM tasks WHERE slug=?", (slug,)).fetchone():
        raise HTTPException(404, "no such task")
    if m.type == "proposal" and not (len(m.refs) == 1 and PIN.match(m.refs[0])):
        raise HTTPException(400, "a proposal has exactly one ref: the pin the trunk should become")
    return {"id": _insert(slug, who, m.agent, m.type, m.body, m.refs, m.parent)}


def _insert(task, author, agent, type, body, refs, parent):
    with lock:
        cur = db.execute("INSERT INTO messages (task,author,agent,ts,type,body,refs,parent) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (task, author, agent, now(), type, body, json.dumps(refs), parent))
        db.commit()
        return cur.lastrowid


@app.get("/messages")
def messages(task: str | None = None, since: int = 0):
    q, args = "SELECT * FROM messages WHERE id > ?", [since]
    if task:
        q, args = q + " AND task = ?", args + [task]
    return [{**dict(r), "refs": json.loads(r["refs"])} for r in db.execute(q + " ORDER BY id", args)]


# --- trunk advancement: lazy consensus with evidence ---

def proposal_state(p, task):
    """Why a proposal can or cannot be merged yet, computed from messages alone."""
    replies = db.execute("SELECT * FROM messages WHERE parent=? ORDER BY id", (p["id"],)).fetchall()
    if any(r["author"] == PLATFORM for r in replies):
        return "closed"
    endorsers = {r["author"] for r in replies
                 if r["type"] == "endorse" and r["author"] != p["author"] and json.loads(r["refs"])}
    stance = {}  # each objector's latest word counts
    for r in replies:
        if r["type"] in ("object", "withdraw"):
            stance[r["author"]] = r["type"]
    objectors = [a for a, s in stance.items() if s == "object"]
    opened = datetime.fromisoformat(p["ts"])
    if objectors:
        return f"blocked by {', '.join(objectors)}"
    if len(endorsers) < task["threshold"]:
        return f"{len(endorsers)}/{task['threshold']} endorsements with evidence"
    if datetime.now(timezone.utc) < opened + timedelta(hours=task["window_hours"]):
        return f"window open until {(opened + timedelta(hours=task['window_hours'])).isoformat()}"
    return "ready"


@app.get("/proposals/{pid}")
def get_proposal(pid: int):
    p = db.execute("SELECT * FROM messages WHERE id=? AND type='proposal'", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "no such proposal")
    t = db.execute("SELECT * FROM tasks WHERE slug=?", (p["task"],)).fetchone()
    return {"id": pid, "state": proposal_state(p, t)}


def merge(p, task):
    """Make the base repo's tree equal to the pinned tree, as one new commit. Never rewrites history,
    so a revert is just a proposal that pins an older state of the base repo. Hugging Face always keeps
    `.gitattributes` (repos need it for large files), so that file is the one exception to "equal"."""
    pin = json.loads(p["refs"])[0]
    with tempfile.TemporaryDirectory() as d:
        checkout(pin, d)
        info = api.upload_folder(repo_id=task["repo"], repo_type="dataset", folder_path=d,
                                 ignore_patterns=[".git/*", ".cache/*"], delete_patterns="*",
                                 commit_message=f"Merge proposal #{p['id']} by {p['author']}",
                                 commit_description=f"{p['body']}\n\nSource: {pin}")
    return f"{base_url(task)}@{info.oid}"


def advance_trunks():
    for p in db.execute("SELECT * FROM messages WHERE type='proposal' ORDER BY id").fetchall():
        task = db.execute("SELECT * FROM tasks WHERE slug=?", (p["task"],)).fetchone()
        if proposal_state(p, task) != "ready":
            continue
        try:
            _insert(p["task"], PLATFORM, "", "merged", "Trunk advanced.", [merge(p, task)], p["id"])
        except Exception as e:
            _insert(p["task"], PLATFORM, "", "merge-failed", f"{type(e).__name__}: {e}"[:2000], [], p["id"])


def dump():
    """Publish the full message log, so the record survives the platform."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in messages()))
        Path(d, "tasks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in list_tasks()))
        api.create_repo(f"{ORG}/platform-log", repo_type="dataset", exist_ok=True)
        api.upload_folder(repo_id=f"{ORG}/platform-log", repo_type="dataset", folder_path=d,
                          commit_message=f"Log dump {now()}")


def loop():
    last_dump = 0
    while True:
        try:
            advance_trunks()
            if time.time() - last_dump > 86400:
                dump()
                last_dump = time.time()
        except Exception as e:
            print("loop:", e, flush=True)
        time.sleep(int(os.environ.get("AIC_TICK", "60")))


if os.environ.get("AIC_LOOP", "1") == "1":
    threading.Thread(target=loop, daemon=True).start()
