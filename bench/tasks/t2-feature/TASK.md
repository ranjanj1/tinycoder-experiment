Add two methods to `Inventory` in `inventory.py`:

1. `remove(name, qty)`: takes qty (a positive int) of an item out of stock. It raises `KeyError` if the item is not
   in stock, and `ValueError` if qty is not a positive integer or is more than the amount in stock. When an item
   reaches 0 it no longer appears in `items()`.
2. `total()`: the number of units in stock across all items.

Add tests for both. Keep the existing tests passing.
