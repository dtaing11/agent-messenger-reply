-- Initial schema (plan section 12). Money is integer cents; timestamps are UTC ISO-8601.

CREATE TABLE customers (
    id            INTEGER PRIMARY KEY,
    page_id       TEXT NOT NULL,
    facebook_psid TEXT NOT NULL,
    name          TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (page_id, facebook_psid)
);

CREATE TABLE conversations (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL UNIQUE REFERENCES customers(id),
    state        TEXT NOT NULL DEFAULT 'IDLE',
    bot_enabled  INTEGER NOT NULL DEFAULT 1,
    handoff_reason TEXT,
    summary      TEXT,
    last_customer_message_at TEXT,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE messages (
    id                  INTEGER PRIMARY KEY,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id),
    external_message_id TEXT,
    role                TEXT NOT NULL CHECK (role IN ('customer','assistant','staff','system')),
    content             TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE UNIQUE INDEX ux_messages_external ON messages(external_message_id) WHERE external_message_id IS NOT NULL;
CREATE INDEX ix_messages_conversation ON messages(conversation_id, id);

CREATE TABLE menu_items (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    unit_label  TEXT NOT NULL DEFAULT 'each',
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    max_quantity INTEGER NOT NULL DEFAULT 100 CHECK (max_quantity > 0),
    available   INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE menu_modifiers (
    id                TEXT PRIMARY KEY,
    item_id           TEXT NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    price_delta_cents INTEGER NOT NULL DEFAULT 0,
    available         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE carts (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','submitted','abandoned')),
    version     INTEGER NOT NULL DEFAULT 1,
    pickup_time TEXT,
    customer_name TEXT,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- At most one active cart per customer.
CREATE UNIQUE INDEX ux_carts_active ON carts(customer_id) WHERE status = 'active';

CREATE TABLE cart_items (
    id        INTEGER PRIMARY KEY,
    cart_id   INTEGER NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
    item_id   TEXT NOT NULL REFERENCES menu_items(id),
    quantity  INTEGER NOT NULL CHECK (quantity > 0),
    modifiers TEXT NOT NULL DEFAULT '[]',  -- sorted JSON array of modifier ids
    UNIQUE (cart_id, item_id, modifiers)
);

CREATE TABLE confirmation_tokens (
    token        TEXT PRIMARY KEY,
    cart_id      INTEGER NOT NULL REFERENCES carts(id),
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    cart_version INTEGER NOT NULL,
    total_cents  INTEGER NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE orders (
    id             INTEGER PRIMARY KEY,
    customer_id    INTEGER NOT NULL REFERENCES customers(id),
    cart_id        INTEGER NOT NULL REFERENCES carts(id),
    cart_version   INTEGER NOT NULL,
    customer_name  TEXT NOT NULL,
    location       TEXT NOT NULL,
    pickup_time    TEXT NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    tax_cents      INTEGER NOT NULL,
    total_cents    INTEGER NOT NULL,
    status         TEXT NOT NULL,
    payment_method TEXT NOT NULL DEFAULT 'pay_at_pickup',
    notes          TEXT NOT NULL DEFAULT '',
    needs_review   INTEGER NOT NULL DEFAULT 0,
    expires_at     TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- A given cart version can become at most one order (duplicate-confirmation guard).
    UNIQUE (cart_id, cart_version)
);
CREATE INDEX ix_orders_status ON orders(status);

CREATE TABLE order_items (
    id                 INTEGER PRIMARY KEY,
    order_id           INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_id            TEXT NOT NULL,
    item_name_snapshot TEXT NOT NULL,
    quantity           INTEGER NOT NULL,
    unit_price_cents   INTEGER NOT NULL,
    modifiers_snapshot TEXT NOT NULL DEFAULT '[]',
    line_total_cents   INTEGER NOT NULL
);

CREATE TABLE order_events (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE webhook_events (
    id                INTEGER PRIMARY KEY,
    provider          TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    customer_key      TEXT,          -- serialization key, e.g. page:psid
    event_type        TEXT NOT NULL,
    payload           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','processing','done','failed','ignored')),
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    locked_at         TEXT,
    last_error        TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (provider, external_event_id)
);
CREATE INDEX ix_webhook_events_queue ON webhook_events(status, next_attempt_at);
CREATE INDEX ix_webhook_events_customer ON webhook_events(customer_key, status);

CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY,
    order_id        INTEGER REFERENCES orders(id),
    type            TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    body            TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    provider_id     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sending','sent','delivered','failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX ix_notifications_queue ON notifications(status, next_attempt_at);

CREATE TABLE knowledge_documents (
    id              TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    category        TEXT NOT NULL,
    location        TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    approval_status TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK (approval_status IN ('pending_review','approved','rejected','retired')),
    effective_from  TEXT,
    effective_until TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (id, version)
);

-- FTS5 over approved knowledge only; kept in sync by triggers. Retrieval lands in Phase 3.
CREATE VIRTUAL TABLE knowledge_fts USING fts5(doc_id UNINDEXED, version UNINDEXED, question, answer, category);

CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge_documents WHEN NEW.approval_status = 'approved'
BEGIN
    INSERT INTO knowledge_fts (doc_id, version, question, answer, category)
    VALUES (NEW.id, NEW.version, NEW.question, NEW.answer, NEW.category);
END;

CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge_documents
BEGIN
    DELETE FROM knowledge_fts WHERE doc_id = OLD.id AND version = OLD.version;
    INSERT INTO knowledge_fts (doc_id, version, question, answer, category)
    SELECT NEW.id, NEW.version, NEW.question, NEW.answer, NEW.category
    WHERE NEW.approval_status = 'approved';
END;

CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge_documents
BEGIN
    DELETE FROM knowledge_fts WHERE doc_id = OLD.id AND version = OLD.version;
END;
