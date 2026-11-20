-- Milk Tea and Green Tea ask how sweet (owner, 2026-11-20). The live menu was edited in the
-- portal, so data/menu.json isn't re-imported there: add the choice to existing rows too.
UPDATE menu_items
SET choices = json_insert(COALESCE(NULLIF(choices, ''), '[]'), '$[#]', json('{"key": "sweetness", "label": "Sweetness", "options": ["Less sweet", "Regular", "More sweet"], "question": "How sweet would you like it: Less sweet, Regular or More sweet?"}'))
WHERE id IN ('milk_tea', 'green_tea')
  AND (choices IS NULL OR choices NOT LIKE '%"sweetness"%');
