-- 3 AM reminder texts to the owners on the pickup day, for orders placed on an earlier day.

ALTER TABLE orders ADD COLUMN day_reminder_sent_at TEXT;
