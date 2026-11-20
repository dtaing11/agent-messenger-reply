-- Photos customers send on Messenger (owner, 2026-11-20: simple version, no image AI).
-- A photo sent while ordering a Special Design Donut is kept with that cart and then the
-- order, so the owners see it in the alert and the portal. Files live in the uploads folder
-- next to the database; `path` is relative to it.
CREATE TABLE customer_photos (
    id                  INTEGER PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customers(id),
    message_external_id TEXT NOT NULL,
    seq                 INTEGER NOT NULL DEFAULT 0,
    path                TEXT,
    mime                TEXT,
    bytes               INTEGER,
    sha256              TEXT,
    cart_id             INTEGER REFERENCES carts(id),
    order_id            INTEGER REFERENCES orders(id),
    error               TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (message_external_id, seq)
);
CREATE INDEX idx_customer_photos_customer ON customer_photos (customer_id, id);
CREATE INDEX idx_customer_photos_cart ON customer_photos (cart_id);
CREATE INDEX idx_customer_photos_order ON customer_photos (order_id);

-- Messenger only: photo files (JSON list of paths in the uploads folder) sent before the text.
ALTER TABLE notifications ADD COLUMN attachments TEXT;

-- Toppings are offered once per order (owner, 2026-11-20): remember when Sprinkle was told to offer them.
ALTER TABLE carts ADD COLUMN topping_offered_at TEXT;
