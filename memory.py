"""The TiDB side of the demo: connect, store one turn, recall with hybrid search."""
import os
import re
import time
from pathlib import Path

import certifi
import pymysql
from dotenv import load_dotenv

# Use this folder's .env, and let it win over anything already exported in the shell.
load_dotenv(Path(__file__).with_name(".env"), override=True)

RRF_K = 60  # the usual reciprocal rank fusion constant

REGIONS_HINT = (
    "This demo needs TiDB Cloud Starter on AWS in a region with full-text search: "
    "us-west-2, us-east-1, ap-northeast-1, eu-central-1, or ap-southeast-1 "
    "(https://docs.pingcap.com/ai/vector-search-full-text-search-sql/). "
    "TiDB Cloud Zero does not let you pick a region; if setup fails there, use Starter."
)
SETUP_FIRST = "Run `python demo.py setup` first"


def database():
    name = os.getenv("TIDB_DATABASE") or "agent_memory_demo"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise SystemExit(f"TIDB_DATABASE may use letters, digits, _ and - only (got {name!r}).")
    return name


def _config(with_database=True):
    missing = [k for k in ("TIDB_HOST", "TIDB_USERNAME", "TIDB_PASSWORD") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)}. Copy .env.example to .env and fill it in "
                         "(or run `python demo.py zero`).")
    config = dict(
        host=os.environ["TIDB_HOST"],
        port=int(os.getenv("TIDB_PORT") or 4000),
        user=os.environ["TIDB_USERNAME"],
        password=os.environ["TIDB_PASSWORD"],
        ssl={"ca": certifi.where()},   # TiDB Cloud requires TLS
        autocommit=True,
        charset="utf8mb4",
        connect_timeout=20,
    )
    if with_database:
        config["database"] = database()
    return config


def _connect(with_database=True):
    try:
        return pymysql.connect(**_config(with_database))
    except pymysql.err.OperationalError as e:
        if e.args and e.args[0] == 1049:   # unknown database
            raise SystemExit(f"{SETUP_FIRST} (database `{database()}` does not exist yet).")
        raise SystemExit(f"Could not connect to TiDB: {e}\nCheck the values in .env.")


def connect():
    """Connect for remember, recall, and reset, and say so if setup has not run."""
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE 'agent_memory'")
        if not cur.fetchone():
            raise SystemExit(f"{SETUP_FIRST} (table agent_memory does not exist yet).")
    return conn


def setup():
    """Create the database and table if needed, then wait for the full-text index."""
    with _connect(with_database=False) as conn, conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{database()}`")
    ddl = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    ddl = re.sub(r"--[^\n]*", "", ddl).strip().rstrip(";")
    with _connect() as conn, conn.cursor() as cur:
        try:
            cur.execute(ddl)
        except pymysql.err.MySQLError as e:
            raise SystemExit(f"Could not create the table: {e}\n{REGIONS_HINT}")
        # CREATE TABLE IF NOT EXISTS keeps an existing table as it is, so check it has what we need.
        cur.execute("SHOW CREATE TABLE agent_memory")
        definition = cur.fetchone()[1].upper()
        needed = {"user_id column": "`USER_ID`", "session_id column": "`SESSION_ID`",
                  "auto-embedding column": "EMBED_TEXT(",
                  "vector index": "VECTOR INDEX", "full-text index": "FULLTEXT INDEX"}
        missing = [name for name, marker in needed.items() if marker not in definition]
        if missing:
            raise SystemExit(f"`{database()}`.agent_memory already exists without the {', '.join(missing)} "
                             "(an older table?). Drop it, or set TIDB_DATABASE to a new name.")
        # A brand-new full-text index can take a little while before it serves queries.
        deadline, waiting = time.time() + 300, False
        while True:
            try:
                cur.execute("SELECT 1 FROM agent_memory WHERE fts_match_word('ready', content) LIMIT 1")
                cur.fetchall()
                return
            except pymysql.err.MySQLError as e:
                if time.time() > deadline:
                    raise SystemExit(f"The full-text index is not ready after 5 minutes: {e}\n{REGIONS_HINT}")
                if not waiting:
                    print("Waiting for the new full-text index to serve queries (up to 5 minutes)...")
                    waiting = True
                time.sleep(5)


def tokens(text):
    """A rough token count: characters / 4."""
    return max(1, len(text) // 4)


def store(conn, user_id, session_id, speaker, text):
    """Save one turn. TiDB generates the embedding as part of the INSERT."""
    if not text.strip():
        return 0   # nothing to embed
    sql = ("INSERT INTO agent_memory (user_id, session_id, speaker, content, tokens) "
           "VALUES (%s, %s, %s, %s, %s)")
    for attempt in range(4):
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (user_id, session_id, speaker, text, tokens(text)))
            return tokens(text)
        except pymysql.err.OperationalError as e:
            code, msg = e.args[0], str(e.args[-1])
            dropped = code in (2006, 2013)                                  # connection went away
            busy = code == 1105 and "Inference" in msg and "Malformed" not in msg
            if attempt == 3 or not (dropped or busy):
                raise
            time.sleep(2 ** attempt)   # the free embedding model can briefly rate-limit
            if dropped:
                conn.ping(reconnect=True)


# Filtering by user and session in the same query means TiDB computes exact distances over
# just this user's rows instead of using the vector index. That is correct and fast for one
# user's memory. When a filter keeps a large share of a big table, use the pattern the TiDB
# docs show for using the vector index with filters (nearest rows first, then filter):
# https://docs.pingcap.com/ai/vector-search-index/#use-the-vector-index-with-filters
VECTOR_SQL = """
SELECT id, speaker, content, tokens,
       VEC_EMBED_COSINE_DISTANCE(embedding, %s) AS distance
FROM agent_memory
WHERE user_id = %s AND session_id = %s
ORDER BY distance
LIMIT %s"""

KEYWORD_SQL = """
SELECT id, speaker, content, tokens
FROM agent_memory
WHERE user_id = %s AND session_id = %s
  AND fts_match_word(%s, content)
ORDER BY fts_match_word(%s, content) DESC
LIMIT %s"""


def recall(conn, user_id, session_id, question, k=3, candidates=10):
    """Hybrid recall: a vector query and a full-text (BM25) query, merged with RRF."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(VECTOR_SQL, (question, user_id, session_id, candidates))
        by_vector = cur.fetchall()
        cur.execute(KEYWORD_SQL, (user_id, session_id, question, question, candidates))
        by_keyword = cur.fetchall()
    fused = {}
    for leg, rows in (("vector", by_vector), ("full-text", by_keyword)):
        for rank, row in enumerate(rows, 1):
            hit = fused.setdefault(row["id"], {**row, "score": 0.0, "found_by": []})
            hit["score"] += 1 / (RRF_K + rank)
            hit["found_by"].append(f"{leg} #{rank}")
    return sorted(fused.values(), key=lambda r: -r["score"])[:k]   # ties keep vector order


def reset(conn, user_id, session_id):
    """Delete only the rows this demo wrote."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_memory WHERE user_id = %s AND session_id = %s",
                    (user_id, session_id))
        return cur.rowcount
