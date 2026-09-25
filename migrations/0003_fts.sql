-- MemPalace Cloudflare D1 Full-Text Search Schema
-- Enables substring / exact keyword matching via FTS5 trigram tokenizer

CREATE VIRTUAL TABLE IF NOT EXISTS drawers_fts USING fts5(
    id UNINDEXED,
    wing,
    room,
    content,
    tokenize='trigram'
);
