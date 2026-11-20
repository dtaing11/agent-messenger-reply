-- Custom items (e.g. Special Design Donut): per-line details such as the message and design.

ALTER TABLE menu_items ADD COLUMN custom_rules TEXT;  -- JSON, NULL for ordinary items

-- Recreate cart_items so two custom lines with different details can coexist.
CREATE TABLE cart_items_new (
    id        INTEGER PRIMARY KEY,
    cart_id   INTEGER NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
    item_id   TEXT NOT NULL REFERENCES menu_items(id),
    quantity  INTEGER NOT NULL CHECK (quantity > 0),
    modifiers TEXT NOT NULL DEFAULT '[]',
    details   TEXT NOT NULL DEFAULT '{}',  -- canonical JSON object
    UNIQUE (cart_id, item_id, modifiers, details)
);
INSERT INTO cart_items_new (id, cart_id, item_id, quantity, modifiers)
    SELECT id, cart_id, item_id, quantity, modifiers FROM cart_items;
DROP TABLE cart_items;
ALTER TABLE cart_items_new RENAME TO cart_items;

ALTER TABLE order_items ADD COLUMN details_snapshot TEXT NOT NULL DEFAULT '{}';
ALTER TABLE order_items ADD COLUMN surcharge_pending INTEGER NOT NULL DEFAULT 0;
