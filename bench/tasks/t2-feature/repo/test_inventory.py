import unittest

from inventory import Inventory


class InventoryTest(unittest.TestCase):
    def test_add_and_count(self):
        inv = Inventory()
        inv.add("apple", 3)
        inv.add("apple", 2)
        self.assertEqual(inv.count("apple"), 5)
        self.assertEqual(inv.count("pear"), 0)

    def test_add_rejects_bad_qty(self):
        with self.assertRaises(ValueError):
            Inventory().add("apple", 0)

    def test_items_sorted(self):
        inv = Inventory()
        inv.add("pear", 1)
        inv.add("apple", 2)
        self.assertEqual(inv.items(), [("apple", 2), ("pear", 1)])


if __name__ == "__main__":
    unittest.main()
