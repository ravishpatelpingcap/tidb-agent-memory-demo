-- One table holds the agent's whole memory: every turn, as it happens.
-- TiDB embeds `content` on insert (free Amazon Titan model hosted by TiDB Cloud),
-- and the same rows serve SQL filters, vector search, and full-text (BM25) search.
CREATE TABLE IF NOT EXISTS agent_memory (
  id          BIGINT PRIMARY KEY AUTO_RANDOM,
  user_id     VARCHAR(64)  NOT NULL,
  session_id  VARCHAR(64)  NOT NULL,
  speaker     VARCHAR(16)  NOT NULL,          -- 'user' or 'agent'
  content     TEXT         NOT NULL,
  tokens      INT          NOT NULL,          -- rough estimate: characters / 4
  created_at  TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  embedding   VECTOR(1024) GENERATED ALWAYS AS (
                EMBED_TEXT('tidbcloud_free/amazon/titan-embed-text-v2', content)
              ) STORED,
  KEY idx_scope (user_id, session_id),
  VECTOR INDEX idx_embedding ((VEC_COSINE_DISTANCE(embedding))),
  FULLTEXT INDEX idx_content (content) WITH PARSER MULTILINGUAL
);
