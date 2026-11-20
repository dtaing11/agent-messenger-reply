-- When each cart line was added. A Special Design Donut must be ordered on a day before the
-- pickup day (owner), and confirmed by 3 AM on the pickup day; this is the "ordered" time.
ALTER TABLE cart_items ADD COLUMN added_at TEXT;
