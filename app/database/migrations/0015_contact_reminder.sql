-- Owner, 2026-11-20: remind a customer once if they picked items but never gave the pickup
-- person's name and phone number. Set when that reminder is queued, so it's sent once per cart.
ALTER TABLE carts ADD COLUMN contact_reminder_at TEXT;
