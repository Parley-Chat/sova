CREATE TABLE webhooks (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    user_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_by TEXT,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
    FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX idx_webhooks_channel_id ON webhooks (channel_id);
CREATE UNIQUE INDEX idx_webhooks_token_hash ON webhooks (token_hash);
