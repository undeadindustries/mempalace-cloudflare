-- MemPalace Cloudflare D1 Drawer Registry Schema
-- Complements Cloudflare Vectorize and R2 by maintaining drawer metadata, taxonomy, and exact-duplicate hashes

CREATE TABLE IF NOT EXISTS drawers (
    id TEXT PRIMARY KEY,
    wing TEXT NOT NULL,
    room TEXT NOT NULL,
    r2_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    source_file TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_drawers_wing_room ON drawers(wing, room);
CREATE INDEX IF NOT EXISTS idx_drawers_hash ON drawers(content_hash);
CREATE INDEX IF NOT EXISTS idx_drawers_source ON drawers(source_file);
CREATE INDEX IF NOT EXISTS idx_drawers_created_at ON drawers(created_at);
