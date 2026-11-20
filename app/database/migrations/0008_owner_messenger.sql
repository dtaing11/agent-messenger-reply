-- Owner alerts on Facebook Messenger: owners link their own account to the Page with a
-- one-time code from the portal, then get order alerts and can reply with commands there.

CREATE TABLE owner_accounts (
    id               INTEGER PRIMARY KEY,
    page_id          TEXT NOT NULL,
    psid             TEXT NOT NULL,
    label            TEXT NOT NULL,
    linked_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- Meta lets the Page message someone only within 24 hours of their last message.
    last_message_at  TEXT,
    keepalive_sent_at TEXT,
    active           INTEGER NOT NULL DEFAULT 1,
    UNIQUE (page_id, psid)
);

CREATE TABLE owner_link_codes (
    code_hash   TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

ALTER TABLE notifications ADD COLUMN channel TEXT NOT NULL DEFAULT 'sms';
-- Messenger only: tap-to-reply buttons, JSON [[title, command], ...]
ALTER TABLE notifications ADD COLUMN buttons TEXT;
