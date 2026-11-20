-- Owner, 2026-11-20: the board's "Filled Donut/Long John" is two items.
--   Long John: Chocolate or Maple, $2.09 (so it's one of the Big Donuts).
--   Filled Donut: an outside (Glazed, Chocolate Icing, Powdered Sugar) and a filling
--   (Bavarian Cream, Blueberry, Raspberry, Lemon, Strawberry jelly), at the board price.
-- The live menu was edited in the portal, so data/menu.json isn't re-imported there.
-- The old item is archived (past orders keep their own copy of the name and price).
INSERT INTO menu_items (id, name, category, description, unit_label, price_cents, max_quantity, available, sort_order, choices)
SELECT 'filled_donut', 'Filled Donut', category, 'Filled donut. Outside: Glazed, Chocolate Icing or Powdered Sugar. Filling: Bavarian Cream, or Blueberry, Raspberry, Lemon or Strawberry jelly. Any outside goes with any filling.', unit_label, price_cents,
       max_quantity, available, sort_order, '[{"key": "outside", "label": "Outside", "options": ["Glazed", "Chocolate Icing", "Powdered Sugar"], "question": "Which outside would you like on the Filled Donut: Glazed, Chocolate Icing or Powdered Sugar?"}, {"key": "filling", "label": "Filling", "options": ["Bavarian Cream", "Blueberry", "Raspberry", "Lemon", "Strawberry"], "question": "Which filling would you like: Bavarian Cream, Blueberry, Raspberry, Lemon or Strawberry jelly?"}]'
FROM menu_items WHERE id = 'filled_donut_long_john' AND NOT EXISTS (SELECT 1 FROM menu_items WHERE id = 'filled_donut');

INSERT INTO menu_items (id, name, category, description, unit_label, price_cents, max_quantity, available, sort_order, choices)
SELECT 'long_john', 'Long John', category, 'Long John filled with Bavarian cream. Flavors: Chocolate or Maple.', unit_label, 209,
       max_quantity, available, sort_order, '[{"key": "flavor", "label": "Flavor", "options": ["Chocolate", "Maple"]}]'
FROM menu_items WHERE id = 'filled_donut_long_john' AND NOT EXISTS (SELECT 1 FROM menu_items WHERE id = 'long_john');

UPDATE menu_items SET archived = 1, available = 0
WHERE id = 'filled_donut_long_john' AND EXISTS (SELECT 1 FROM menu_items WHERE id = 'long_john');
