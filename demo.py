"""Context compaction vs. a TiDB memory table, reproduced in a terminal.

  python demo.py setup      create the agent_memory table (safe to re-run)
  python demo.py forget     run 1: small context window + a notes file. Compaction loses the date.
  python demo.py remember   run 2: the same chat, every turn also saved in TiDB. Recall finds it.
  python demo.py recall     a brand-new process, empty context, asks again. Only TiDB can answer.
                            Add your own question: python demo.py recall "What date is Atlas shipping?"
  Add --step to forget or remember to play the chat one exchange at a time.
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


def pause(step):
    """With --step, wait for Enter so the chat plays one exchange at a time."""
    if step and sys.stdin.isatty():
        input("         (press Enter to continue)")


def shown(text, n=110):
    """One line for the terminal; long saved replies are cut here, never in TiDB."""
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[:n].rstrip() + f"... ({len(text):,} chars saved)"


def run_chat(conn=None, step=False):
    """Play the scripted chat. With a connection, every message is also saved in TiDB, in full."""
    context, spent, summary = [], 0, []
    notes = ["# Things to remember"]   # the agent's memory.md, kept as a list here
    for turn in chat.TURNS:
        for who, text, saved in (("you", turn["user"], turn["user"]),
                                 ("agent", turn["agent"], turn["stored"])):
            tok = memory.tokens(saved)   # counted from the full text, the same way TiDB stores it
            say(who, text, tok)
            if saved != text:
                note("shortened here, saved in full" if conn else "shortened here")
            context.append((saved, tok))
            if conn:
                tok = memory.store(conn, chat.USER_ID, chat.SESSION_ID,
                                   "user" if who == "you" else "agent", saved)
                note(f"saved to TiDB agent_memory ({tok:,} tokens as stored)")
            spent += tok
        if turn.get("note"):
            notes.append(f"- {turn['note']}")
            note(f'memory.md += "{turn["note"]}"')
        if turn.get("summary"):
            summary.append(turn["summary"])
        used = sum(t for _, t in context)
        note(f"context window: {used:,} / {chat.WINDOW:,} tokens")
        pause(step)
        print()
        if used > chat.WINDOW:
            kept = context[-2:]
            print(f"== Context window full ({used:,} / {chat.WINDOW:,}). The agent compacts:")
            note(f'summary kept : "I {"; ".join(summary)}."')
            note(f'kept verbatim: the latest exchange ("{kept[0][0]}" and the reply)')
            note(f'deleted      : every older message, including "...we ship on {chat.FACT}."')
            if conn:
                note("TiDB still has all 8 rows in agent_memory.")
            context = [("I " + "; ".join(summary) + ".", 0)] + kept
            pause(step)
            print()
    if conn:
        print("Saved: your 4 messages and its 4 replies, in full. Your question is searched, not saved.")
    q_tok = memory.tokens(chat.QUESTION)
    say("you", chat.QUESTION, q_tok)
    spent += q_tok
    return context, notes, spent


def cmd_setup(_):
    memory._config()   # fail fast if .env is missing
    print(f"Creating database `{memory.database()}` and table agent_memory (safe to re-run)...")
    memory.setup()
    print("Ready: agent_memory with auto-embedding, a vector index, and a full-text index.")


def cmd_forget(args):
    line("=")
    print("RUN 1 (scripted chat, no LLM): a 6,000-token context window plus a memory.md notes file.")
    print("No database.")
    line("=")
    context, notes, spent = run_chat(step=args.step)
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


def cmd_remember(args):
    conn = memory.connect()
    cleared = memory.reset(conn, chat.USER_ID, chat.SESSION_ID)
    line("=")
    print("RUN 2 (same scripted chat): the same window, but every message is also saved in TiDB.")
    if cleared:
        print(f"(cleared {cleared} rows from a previous run)")
    line("=")
    _, _, spent = run_chat(conn, step=args.step)
    answer(conn, spent, saved_rows=2 * len(chat.TURNS))


def answer(conn, spent=None, question=chat.QUESTION, saved_rows=None, old_chat=False):
    t0 = time.perf_counter()
    rows = memory.recall(conn, chat.USER_ID, chat.SESSION_ID, question)
    ms = (time.perf_counter() - t0) * 1000
    print()
    if not rows:
        say("agent", "I could not find that in my memory table.")
        print("(The table has no rows for this demo. Run `python demo.py remember` first.)")
        return
    print(f"Hybrid recall over agent_memory (vector + full-text, fused with RRF), {ms:.0f} ms:")
    for i, r in enumerate(rows, 1):
        note(f'{i}. [{r["speaker"]}, {r["tokens"]:,} tokens, {" + ".join(r["found_by"])}] {shown(r["content"])}')
    recalled = sum(r["tokens"] for r in rows)
    print()
    if question != chat.QUESTION:
        # Your own question: there is no scripted answer, so just show what TiDB found.
        say("agent", f'Closest match in my memory table: "{shown(rows[0]["content"])}"')
        print(f"Recall read back {recalled:,} tokens from TiDB.")
        return
    hit = next((r for r in rows if chat.FACT in r["content"]), None)   # any returned row, not just #1
    if hit is None:
        say("agent", "I could not find that in my memory table.")
        return
    say("agent", f"Atlas ships on {chat.FACT}. Found it in my memory table "
                 f'(result {rows.index(hit) + 1} of {len(rows)}): "{shown(hit["content"])}"')
    print()
    if not spent:
        print(f"Recall read back {recalled:,} tokens from TiDB.")
        return
    ratio = int(spent / recalled + 0.5)   # rounds half up
    which = "The old chat" if old_chat else "The whole chat"
    print(f"{which} is about {spent:,} tokens: the {saved_rows} rows as saved plus your question. "
          f"Recall read back {recalled:,} tokens from TiDB, about {ratio}x fewer.")
    print("Both counts use the same estimate (characters / 4) over the same saved text. "
          "One scripted chat, not a benchmark.")


def cmd_recall(args):
    conn = memory.connect()
    question = args.question or chat.QUESTION
    print("A brand-new process: no conversation, no summary, no notes. Only the table.")
    say("you", question)
    saved_rows, saved_tokens = memory.chat_tokens(conn, chat.USER_ID, chat.SESSION_ID)
    answer(conn, spent=saved_tokens + memory.tokens(question), question=question,
           saved_rows=saved_rows, old_chat=True)


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
        if name in ("forget", "remember"):
            sp.add_argument("--step", action="store_true", help="pause after each exchange")
        if name == "recall":
            sp.add_argument("question", nargs="?", help="your own question (default: the Atlas one)")
    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit("\nStopped.")


if __name__ == "__main__":
    main()
