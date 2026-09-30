# Agent memory that survives context compaction

A small, runnable companion to the blog post *Context Compaction: Why AI Agents Forget and How a
Memory Table Fixes It*. It reproduces the post's result in a terminal:

1. **Run 1:** an agent with a small context window and a `memory.md` notes file is told
   "we ship on Oct 12". After compaction, neither its summary nor its notes has the date.
2. **Run 2:** the same chat, but every message is also saved as a row in one TiDB table.
   A hybrid search (vector + full-text) finds the original sentence.
3. **Restart:** a brand-new process with an empty context asks again, and TiDB still answers.

**What's real and what's scripted.** The chat is scripted, so every run is identical and no LLM or
API key is needed. The replies, the notes the agent writes to `memory.md` (a list kept in memory,
not a file on disk), when compaction happens, and the summary are all fixed. The long replies carry
set token sizes but are stored as one-line stand-ins. The agent's closing lines are templates too:
run 1 checks whether "Oct 12" is still anywhere in its compacted context or notes, and run 2 checks
whether the top recalled row contains "Oct 12", then states the date and quotes that row. No model
reads the row. The TiDB part is real: each message is inserted, TiDB embeds it on insert, and recall
runs real vector and full-text queries against the table.

## You need

- Python 3.10 or later (3.9 also works)
- A TiDB Cloud database. Pick one:
  - **TiDB Cloud Starter** (free). Create it on **AWS** in a region that supports full-text search:
    Oregon (`us-west-2`), N. Virginia (`us-east-1`), Tokyo (`ap-northeast-1`), Frankfurt
    (`eu-central-1`), or Singapore (`ap-southeast-1`). Check the
    [current list](https://docs.pingcap.com/ai/vector-search-full-text-search-sql/).
  - **Or no signup:** `python demo.py zero` creates a free, disposable
    [TiDB Cloud Zero](https://zero.tidbcloud.com/) database and writes `.env` for you. It expires
    after 30 days. The link to keep it is saved in `.env`; treat it like a password. You cannot
    choose Zero's region, so if `setup` fails on Zero, use Starter instead.

No embedding API key is needed. The table uses the free Amazon Titan model that TiDB Cloud hosts.

## Run it

```bash
git clone https://github.com/pingcap/tidb-agent-memory-demo.git
cd tidb-agent-memory-demo
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then connect a database, with **one** of these:

```bash
# Option A, TiDB Cloud Starter:
cp .env.example .env               # Windows: copy .env.example .env
# ...then paste your host, port, username, and password into .env

# Option B, no signup:
python demo.py zero
```

For Option A, open your Starter instance, click **Connect**, and copy the host, port, username,
and password (click **Generate Password** if you have not set one). Leave
`TIDB_DATABASE=agent_memory_demo` as it is. `setup` creates that database.

Then run the demo:

```bash
python demo.py setup          # creates the table (safe to re-run)
python demo.py forget         # run 1: compaction loses the date
python demo.py remember       # run 2: TiDB recalls it
python demo.py recall         # new process, empty context: still recalled
python demo.py reset          # delete this demo's rows
```

Two options:

- `python demo.py forget --step` (or `remember --step`) plays the chat one exchange at a time.
  Press Enter to continue.
- `python demo.py recall "What date is Atlas shipping?"` asks your own question. TiDB runs the
  same hybrid search and shows the closest stored messages. Only the default question gets a
  scripted answer line.

## What you should see

`python demo.py forget` plays the chat turn by turn, then ends like this:

```
== Context window full (7,554 / 6,000). The agent compacts:
         summary kept : "I prepped the Atlas launch; summarized the vendor report; planned the 3-day offsite."
         kept verbatim: the latest exchange ("Plan a 3-day team offsite." and the reply)
         deleted      : every older message, including "...we ship on Oct 12."

   you > Remind me, when do we ship Atlas?  (~8 tokens)
 agent > My summary says we worked on the Atlas launch, but there is no date in it, and my notes file has your name, birthday, and role. Could you tell me the date again?

Forgotten. The summary kept the task, not the date, and memory.md never had it:
         # Things to remember
         - Maya, birthday March 14
         - Works as a Product Manager
Getting it back means re-reading the whole chat, about 7,562 tokens, if a transcript was kept.
```

`python demo.py remember` plays the same chat, saves each message, and ends like this:

```
Hybrid recall over agent_memory (vector + full-text, fused with RRF), 655 ms:
         1. [user, 18 tokens, vector #1 + full-text #1] I'm a Product Manager. Help me prep the Atlas launch, we ship on Oct 12.
         2. [agent, 29 tokens, vector #2 + full-text #3] On it! Atlas launch prep: messaging doc, pricing page, beta-customer quotes, and a launch-day checklist... [full plan]
         3. [agent, 20 tokens, vector #3 + full-text #2] Nice to meet you! Let me note that down. So, what are you working on these days?

 agent > Atlas ships on Oct 12. Found it in my memory table: "I'm a Product Manager. Help me prep the Atlas launch, we ship on Oct 12."

This chat cost about 7,562 tokens (scripted reply sizes). Recall pulled back 67 tokens from TiDB, about 113x fewer than re-reading it.
```

`python demo.py recall` starts a new process with no conversation at all. It returns the same
three rows and answer, and ends with `Recall pulled back 67 tokens from TiDB.` Timings vary with
your region and whether the instance was idle.

## How it works

The whole memory is one table ([`schema.sql`](schema.sql)):

```sql
CREATE TABLE IF NOT EXISTS agent_memory (
  id          BIGINT PRIMARY KEY AUTO_RANDOM,
  user_id     VARCHAR(64)  NOT NULL,
  session_id  VARCHAR(64)  NOT NULL,
  speaker     VARCHAR(16)  NOT NULL,
  content     TEXT         NOT NULL,
  tokens      INT          NOT NULL,
  created_at  TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  embedding   VECTOR(1024) GENERATED ALWAYS AS (
                EMBED_TEXT('tidbcloud_free/amazon/titan-embed-text-v2', content)
              ) STORED,
  KEY idx_scope (user_id, session_id),
  VECTOR INDEX idx_embedding ((VEC_COSINE_DISTANCE(embedding))),
  FULLTEXT INDEX idx_content (content) WITH PARSER MULTILINGUAL
);
```

Saving a turn is a plain `INSERT` of the text. TiDB generates the embedding. Recall
([`memory.py`](memory.py)) runs two queries against the same rows and merges them with reciprocal
rank fusion (each row scores `1 / (60 + rank)` per list; rows 2 and 3 above tie, and ties keep
vector order):

```sql
-- by meaning: TiDB embeds the question text itself
SELECT id, content, VEC_EMBED_COSINE_DISTANCE(embedding, 'Remind me, when do we ship Atlas?') AS distance
FROM agent_memory WHERE user_id = 'maya' AND session_id = 'blog-demo'
ORDER BY distance LIMIT 10;

-- by keyword (BM25)
SELECT id, content FROM agent_memory
WHERE user_id = 'maya' AND session_id = 'blog-demo'
  AND fts_match_word('Remind me, when do we ship Atlas?', content)
ORDER BY fts_match_word('Remind me, when do we ship Atlas?', content) DESC LIMIT 10;
```

Every recall query is scoped to one user and session, so recall never returns another user's rows.
The application enforces that scope in its `WHERE` clause. Because the filter sits in the same
query as the vector ordering, TiDB computes exact distances over just that user's rows rather than
using the vector index. That is correct and fast for one user's memory. When a filter keeps a large
share of a big table, use the pattern the TiDB docs show for
[using the vector index with filters](https://docs.pingcap.com/ai/vector-search-index/#use-the-vector-index-with-filters):
fetch the nearest rows first, then filter.

## About the numbers

- **7,562 tokens** is the scripted size of the chat: the four exchanges' fixed budgets
  (34 + 420 + 3,900 + 3,200 = 7,554) plus the 8-token question. The stored rows are one-line
  stand-ins, 165 tokens for all eight.
- **67 tokens** is the size of the three rows the search returned (18 + 29 + 20), from the
  `tokens` column, estimated as characters / 4.
- 7,562 / 67 is about 113x. The ratio models what re-reading a chat that size would cost. It is
  not a benchmark.

## Clean up

- `python demo.py reset` deletes only this demo's rows.
- To remove everything, delete the Starter instance, or drop the database named in your
  `TIDB_DATABASE` (`agent_memory_demo` by default) from the TiDB Cloud **SQL Editor** or any
  MySQL client, for example `DROP DATABASE agent_memory_demo;`. Check the name first.
- A TiDB Cloud Zero database removes itself after 30 days.
- Delete `.env` when you are done. It holds the password.

## Troubleshooting

- **`Could not create the table`, or an error about `EMBED_TEXT` or full-text:** the instance is
  not TiDB Cloud Starter on AWS in a supported region. See *You need* above.
- **`Run python demo.py setup first`:** the database or table does not exist yet.
- **`Could not connect to TiDB`:** check the host, port, username, and password in `.env`. Values in
  `.env` take priority over variables exported in your shell.
- **Setup waits a while:** a new full-text index can take a moment before it serves queries. Setup
  waits up to five minutes.
- **An insert pauses and retries:** the free embedding model can briefly rate-limit. The demo
  retries automatically.
