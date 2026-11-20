-- Fixes from the break test (2026-11-20).

-- A Messenger account linked with a code only gets alerts and can send commands after someone
-- approves it in the portal. Accounts linked before this migration count as approved.
ALTER TABLE owner_accounts ADD COLUMN approved_at TEXT;
UPDATE owner_accounts SET approved_at = linked_at;

-- Link attempts, for limiting wrong guesses per account and overall.
CREATE TABLE owner_link_attempts (
    id    INTEGER PRIMARY KEY,
    psid  TEXT NOT NULL,
    ok    INTEGER NOT NULL,
    at    TEXT NOT NULL
);
CREATE INDEX ix_owner_link_attempts_at ON owner_link_attempts(at);

-- The cart as it was before the assistant handled a customer message. If handling fails part way
-- (crash or error) the retry starts from this state, so items are never added twice.
CREATE TABLE agent_turns (
    message_id  TEXT PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    snapshot    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
