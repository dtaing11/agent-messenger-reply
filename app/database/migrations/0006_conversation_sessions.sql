-- Fresh start for returning customers: after a quiet gap, the assistant only sees messages
-- from the new session, and an unfinished cart is set aside until the customer decides.

ALTER TABLE conversations ADD COLUMN session_start_message_id INTEGER;
ALTER TABLE conversations ADD COLUMN set_aside_cart_id INTEGER REFERENCES carts(id);
