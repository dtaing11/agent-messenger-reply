-- Customer profiles: phone number, owner-only pickup history (no-shows), pickup checks.

ALTER TABLE customers ADD COLUMN phone TEXT;            -- E.164, latest one the customer gave
ALTER TABLE carts ADD COLUMN customer_phone TEXT;
ALTER TABLE orders ADD COLUMN customer_phone TEXT;      -- snapshot at order time
ALTER TABLE orders ADD COLUMN pickup_check_sent_at TEXT; -- when owners were asked "picked up?"
CREATE INDEX ix_orders_customer ON orders(customer_id, id);

-- Required choices on menu items, e.g. Twist flavor: [{"key","label","options":[...]}]
ALTER TABLE menu_items ADD COLUMN choices TEXT;
