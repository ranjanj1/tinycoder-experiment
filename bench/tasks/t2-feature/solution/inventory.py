"""A tiny in-memory stock list."""


class Inventory:
    def __init__(self):
        self._stock = {}

    def add(self, name, qty):
        """Add qty (a positive int) of item name."""
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError("qty must be a positive integer")
        self._stock[name] = self._stock.get(name, 0) + qty

    def count(self, name):
        """How many of name are in stock (0 if none)."""
        return self._stock.get(name, 0)

    def items(self):
        """Sorted list of (name, qty) for every item in stock."""
        return sorted(self._stock.items())

    def remove(self, name, qty):
        """Take qty (a positive int) of item name out of stock."""
        if name not in self._stock:
            raise KeyError(name)
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0 or qty > self._stock[name]:
            raise ValueError("qty must be a positive integer no larger than the stock")
        self._stock[name] -= qty
        if self._stock[name] == 0:
            del self._stock[name]

    def total(self):
        """Units in stock across all items."""
        return sum(self._stock.values())
