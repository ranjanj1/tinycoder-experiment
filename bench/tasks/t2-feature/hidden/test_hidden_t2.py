import unittest

from inventory import Inventory


def inv_with(**kw):
    inv = Inventory()
    for k, v in kw.items():
        inv.add(k, v)
    return inv


class HiddenT2(unittest.TestCase):
    def test_remove_some(self):
        inv = inv_with(apple=5)
        inv.remove("apple", 2)
        self.assertEqual(inv.count("apple"), 3)

    def test_remove_all_drops_item(self):
        inv = inv_with(apple=2, pear=1)
        inv.remove("apple", 2)
        self.assertEqual(inv.items(), [("pear", 1)])
        self.assertEqual(inv.count("apple"), 0)

    def test_remove_unknown(self):
        with self.assertRaises(KeyError):
            inv_with(apple=1).remove("pear", 1)

    def test_remove_too_many(self):
        inv = inv_with(apple=1)
        with self.assertRaises(ValueError):
            inv.remove("apple", 2)
        self.assertEqual(inv.count("apple"), 1)

    def test_remove_bad_qty(self):
        inv = inv_with(apple=3)
        for bad in (0, -1, 1.5, "1"):
            with self.assertRaises(ValueError):
                inv.remove("apple", bad)
        self.assertEqual(inv.count("apple"), 3)

    def test_total(self):
        self.assertEqual(Inventory().total(), 0)
        inv = inv_with(apple=2, pear=3)
        self.assertEqual(inv.total(), 5)
        inv.remove("pear", 3)
        self.assertEqual(inv.total(), 2)


if __name__ == "__main__":
    unittest.main()
