"""Context compaction vs. a TiDB memory table, reproduced in a terminal.

  python demo.py setup      create the agent_memory table (safe to re-run)
  python demo.py forget     run 1: small context window + a notes file. Compaction loses the date.
  python demo.py remember   run 2: the same chat, every turn also saved in TiDB. Recall finds it.
  python demo.py recall     a brand-new process, empty context, asks again. Only TiDB can answer.
  python demo.py reset      delete the rows this demo created
  python demo.py zero       optional: get a free, disposable TiDB Cloud Zero database and write .env
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import certifi

import conversation as chat
import memory


def line(char="-"):
    print(char * 78)


def say(who, text, tokens=None):
    size = f"  (~{tokens:,} tokens)" if tokens else ""
    print(f"{who:>6} > {text}{size}")


def note(text):
    print(f"         {text}")


def run_chat(conn=None):
    """Play the scripted chat. With a connection, every message is also saved in TiDB."""
    context, spent, summary = [], 0, []
    notes = ["# Things to remember"]   # the agent's memory.md, kept as a list here
    for turn in chat.TURNS:
        u_tok = memory.tokens(turn["user"])
        a_tok = turn["budget"] - u_tok   # the reply's scripted size
        for who, text, tok in (("you", turn["user"], u_tok), ("agent", turn["agent"], a_tok)):
            say(who, text, tok)
            context.append((text, tok))
            spent += tok
            if conn:
                stored = memory.store(conn, chat.USER_ID, chat.SESSION_ID,
                                      "user" if who == "you" else "agent", text)
                note(f"saved to TiDB agent_memory ({stored} tokens as stored)")
        if turn.get("note"):
            notes.append(f"- {turn['note']}")
            note(f'memory.md += "{turn["note"]}"')
        if turn.get("summary"):
            summary.append(turn["summary"])
        used = sum(t for _, t in context)
        note(f"context window: {used:,} / {chat.WINDOW:,} tokens")
        print()
        if used > chat.WINDOW:
            kept = context[-2:]
            print(f"== Context window full ({used:,} / {chat.WINDOW:,}). The agent compacts:")
            note(f'summary kept : "I {"; ".join(summary)}."')
            note(f'kept verbatim: the latest exchange ("{kept[0][0]}" and the reply)')
            note(f'deleted      : every older message, including "...we ship on {chat.FACT}."')
            context = [("I " + "; ".join(summary) + ".", 0)] + kept
            print()
    q_tok = memory.tokens(chat.QUESTION)
    say("you", chat.QUESTION, q_tok)
    spent += q_tok
    return context, notes, spent


def cmd_setup(_):
    memory._config()   # fail fast if .env is missing
    print(f"Creating database `{memory.database()}` and table agent_memory (safe to re-run)...")
    memory.setup()
    print("Ready: agent_memory with auto-embedding, a vector index, and a full-text index.")


def cmd_forget(_):
    line("=")
    print("RUN 1 (scripted chat, no LLM): a 6,000-token context window plus a memory.md notes file.")
    print("No database.")
    line("=")
    context, notes, spent = run_chat()
    # A real check: is the date anywhere the agent can still see?
    if any(chat.FACT in text for text, _ in context) or any(chat.FACT in n for n in notes):
        say("agent", "Found it.")
        return
    say("agent", "My summary says we worked on the Atlas launch, but there is no date in it, and my "
                 "notes file has your name, birthday, and role. Could you tell me the date again?")
    print()
    print("Forgotten. The summary kept the task, not the date, and memory.md never had it:")
    for n in notes:
        note(n)
    print(f"Getting it back means re-reading the whole chat, about {spent:,} tokens, if a transcript was kept.")


def cmd_remember(_):
    conn = memory.connect()
    cleared = memory.reset(conn, chat.USER_ID, chat.SESSION_ID)
    line("=")
    print("RUN 2 (same scripted chat): the same window, but every message is also saved in TiDB.")
    if cleared:
        print(f"(cleared {cleared} rows from a previous run)")
    line("=")
    _, _, spent = run_chat(conn)
    answer(conn, spent)


def answer(conn, spent=None):
    t0 = time.perf_counter()
    rows = memory.recall(conn, chat.USER_ID, chat.SESSION_ID, chat.QUESTION)
    ms = (time.perf_counter() - t0) * 1000
    print()
    if not rows:
        say("agent", "I could not find that in my memory table.")
        print("(The table has no rows for this demo. Run `python demo.py remember` first.)")
        return
    print(f"Hybrid recall over agent_memory (vector + full-text, fused with RRF), {ms:.0f} ms:")
    for i, r in enumerate(rows, 1):
        note(f'{i}. [{r["speaker"]}, {r["tokens"]} tokens, {" + ".join(r["found_by"])}] {r["content"]}')
    print()
    top = rows[0]
    if chat.FACT not in top["content"]:
        say("agent", "I could not find that in my memory table.")
        return
    say("agent", f'Atlas ships on {chat.FACT}. Found it in my memory table: "{top["content"]}"')
    recalled = sum(r["tokens"] for r in rows)
    print()
    if spent:
        print(f"This chat cost about {spent:,} tokens (scripted reply sizes). Recall pulled back "
              f"{recalled} tokens from TiDB, about {spent / recalled:.0f}x fewer than re-reading it.")
    else:
        print(f"Recall pulled back {recalled} tokens from TiDB.")


def cmd_recall(_):
    conn = memory.connect()
    print("A brand-new process: no conversation, no summary, no notes. Only the table.")
    say("you", chat.QUESTION)
    answer(conn)


def cmd_reset(_):
    n = memory.reset(memory.connect(), chat.USER_ID, chat.SESSION_ID)
    print(f"Deleted {n} rows (user '{chat.USER_ID}', session '{chat.SESSION_ID}'). The table stays.")


def cmd_zero(args):
    env = Path(__file__).with_name(".env")
    text = env.read_text() if env.exists() else ""
    if re.search(r"^TIDB_PASSWORD=\S", text, re.M) and not args.force:
        sys.exit(".env already has connection details. Use `python demo.py zero --force` to replace them.")
    req = urllib.request.Request("https://zero.tidbapi.com/v1beta1/instances", method="POST",
                                 data=json.dumps({"tag": "agent-memory-demo"}).encode(),
                                 headers={"Content-Type": "application/json"})
    tls = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=30, context=tls) as resp:
            inst = json.load(resp)["instance"]
        c = inst["connection"]
        values = {"TIDB_HOST": c["host"], "TIDB_PORT": c["port"], "TIDB_USERNAME": c["username"],
                  "TIDB_PASSWORD": c["password"], "TIDB_DATABASE": "agent_memory_demo"}
    except urllib.error.HTTPError as e:
        sys.exit(f"TiDB Cloud Zero answered HTTP {e.code} {e.reason}. Try again later, "
                 "or use TiDB Cloud Starter and fill in .env by hand.")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Could not reach TiDB Cloud Zero ({e}). Check your network, or use TiDB Cloud Starter.")
    except (KeyError, TypeError, ValueError):
        sys.exit("TiDB Cloud Zero answered in a format this demo does not know. "
                 "Use TiDB Cloud Starter and fill in .env by hand.")
    expires = (inst.get("expiresAt") or "")[:10] or "in 30 days"
    claim = (inst.get("claimInfo") or {}).get("claimUrl")
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    if claim:
        body += f"# Claim before {expires} to keep this database. Keep this link private:\n# {claim}\n"
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # it holds a password
    with os.fdopen(fd, "w") as f:
        f.write(body)
    env.chmod(0o600)   # also tightens an older .env replaced with --force (no effect on Windows)
    print(f"Created a free, disposable TiDB Cloud Zero database and wrote .env (host {c['host']}).")
    print(f"It expires on {expires}.")
    if claim:
        print("To keep it as a regular TiDB Cloud Starter database, open the claim link before then.")
        print("The link is saved in .env. Keep it private: whoever has it can take over the database.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in [("setup", cmd_setup), ("forget", cmd_forget), ("remember", cmd_remember),
                     ("recall", cmd_recall), ("reset", cmd_reset), ("zero", cmd_zero)]:
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        if name == "zero":
            sp.add_argument("--force", action="store_true", help="overwrite existing .env")
    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit("\nStopped.")


if __name__ == "__main__":
    main()
