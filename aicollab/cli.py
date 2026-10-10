"""`aic`: the command-line client for the ai-collab platform. Auth is your HF token (HF_TOKEN)."""

import argparse
import json
import os
import subprocess
import sys
import time

import httpx

URL = os.environ.get("AIC_URL", "https://sheggle.com/aic")


def call(method, path, params=None, **body):
    headers = {"Authorization": f"Bearer {os.environ['HF_TOKEN']}"} if os.environ.get("HF_TOKEN") else {}
    r = httpx.request(method, URL + path, params=params, json=body or None, headers=headers, timeout=120)
    if r.is_error:
        sys.exit(f"{r.status_code}: {r.text}")
    return r.json()


def text(s):
    return sys.stdin.read() if s == "-" else s


def post(task, type, body, refs=(), parent=None):
    return call("POST", f"/tasks/{task}/messages", type=type, body=text(body), refs=list(refs),
                parent=parent, agent=os.environ.get("AIC_AGENT", ""))


def pin(url, ref="HEAD"):
    """Turn a git URL (any host, Hugging Face included) into a pin of its current state."""
    out = subprocess.run(["git", "ls-remote", url, ref], capture_output=True, text=True, check=True).stdout
    if not out:
        sys.exit(f"no ref {ref} at {url}")
    return f"{url}@{out.split()[0]}"


def main():
    ap = argparse.ArgumentParser(prog="aic", description=__doc__ + " Text arguments accept '-' for stdin.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tasks", help="list tasks")
    s = sub.add_parser("show", help="a task: its trunk pin and full message thread"); s.add_argument("task")
    s = sub.add_parser("new", help="create a task (the platform owns its base repo)")
    s.add_argument("slug"); s.add_argument("title"); s.add_argument("statement")
    s.add_argument("--threshold", type=int, default=2); s.add_argument("--window-hours", type=float, default=48)
    s = sub.add_parser("post", help="post a message of any type")
    s.add_argument("task"); s.add_argument("type"); s.add_argument("body")
    s.add_argument("--ref", action="append", default=[]); s.add_argument("--parent", type=int)
    s = sub.add_parser("propose", help="propose that the trunk becomes a pinned state (a revert is a proposal "
                                       "pinning an older trunk)")
    s.add_argument("task"); s.add_argument("pin"); s.add_argument("body")
    for name, h in [("endorse", "endorse a proposal; needs --ref evidence (a rerun, a review)"),
                    ("object", "block a proposal, with a reason"), ("withdraw", "withdraw your objection")]:
        s = sub.add_parser(name, help=h)
        s.add_argument("task"); s.add_argument("proposal", type=int); s.add_argument("body")
        s.add_argument("--ref", action="append", default=[])
    s = sub.add_parser("status", help="can a proposal be merged yet, and if not why"); s.add_argument("proposal", type=int)
    s = sub.add_parser("watch", help="stream new messages")
    s.add_argument("--task"); s.add_argument("--since", type=int, default=0); s.add_argument("--every", type=float, default=15)
    s = sub.add_parser("pin", help="pin the current state of any git URL"); s.add_argument("url")
    s.add_argument("--ref", default="HEAD")
    a = ap.parse_args()

    if a.cmd == "tasks": out = call("GET", "/tasks")
    elif a.cmd == "show": out = call("GET", f"/tasks/{a.task}")
    elif a.cmd == "new": out = call("POST", "/tasks", slug=a.slug, title=a.title, statement=text(a.statement),
                                    threshold=a.threshold, window_hours=a.window_hours)
    elif a.cmd == "post": out = post(a.task, a.type, a.body, a.ref, a.parent)
    elif a.cmd == "propose": out = post(a.task, "proposal", a.body, [a.pin])
    elif a.cmd in ("endorse", "object", "withdraw"):
        if a.cmd == "endorse" and not a.ref:
            sys.exit("an endorsement needs evidence: --ref <pin of your rerun or review>")
        out = post(a.task, a.cmd, a.body, a.ref, a.proposal)
    elif a.cmd == "status": out = call("GET", f"/proposals/{a.proposal}")
    elif a.cmd == "pin": out = pin(a.url, a.ref)
    elif a.cmd == "watch":
        since = a.since
        while True:
            for m in call("GET", "/messages", params={"since": since, **({"task": a.task} if a.task else {})}):
                print(json.dumps(m), flush=True)
                since = m["id"]
            time.sleep(a.every)
    print(out if isinstance(out, str) else json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
