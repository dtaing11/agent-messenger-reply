-- Owner, 2026-11-20: toppings only go on Green Tea, Milk Tea, Iced Coffee and Smoothie (the
-- Bubble Tea drinks). Every other drink (coffee, cappuccino, milk, soda, juice, water, Starbucks,
-- Prime, Tummy Yummy, energy drink) can't have them. The Iced Coffee is the house blend, not
-- Community Coffee (only Coffee and Cappuccino are Community Coffee).
-- The live menu was edited in the portal, so data/menu.json isn't re-imported there. The add-ons
-- are hidden, not deleted, so past orders keep them; a description the owners rewrote is kept
-- and only gets the new fact added in front.
UPDATE menu_modifiers SET available = 0
WHERE item_id IN ('large_milk', 'medium_milk', 'small_milk', 'soft_drink', 'energy_drink', 'juice_minute_maid', 'coffee', 'cappuccino', 'water', 'tummy_yummy', 'starbucks', 'prime')
   OR item_id IN (SELECT id FROM menu_items WHERE category = 'drinks');

UPDATE menu_items SET description = 'Iced coffee made with our house blend (not Community Coffee). Toppings (add any number): Coconut Jelly, Mango, Peach, Passion Fruit or Strawberry Bursting Boba, Tapioca Pearl (+$0.80 each), Coffee Jelly (+$1.25).'
WHERE id = 'iced_coffee' AND TRIM(COALESCE(description, '')) IN ('', 'Bubble tea menu. Toppings (add any number): Coconut Jelly, Mango, Peach, Passion Fruit or Strawberry Bursting Boba, Tapioca Pearl (+$0.80 each), Coffee Jelly (+$1.25).');
UPDATE menu_items SET description = 'Made with our house blend (not Community Coffee). ' || description
WHERE id = 'iced_coffee' AND lower(description) NOT LIKE '%house blend%';
UPDATE menu_items SET search_terms = TRIM(search_terms || ', house blend', ', ')
WHERE id = 'iced_coffee' AND lower(search_terms) NOT LIKE '%house blend%';
