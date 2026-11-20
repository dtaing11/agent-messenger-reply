-- Menu editor in the portal: named, ordered categories; item ordering; removed items kept
-- (archived) when old carts still point at them.

CREATE TABLE menu_categories (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

ALTER TABLE menu_items ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;
ALTER TABLE menu_items ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
