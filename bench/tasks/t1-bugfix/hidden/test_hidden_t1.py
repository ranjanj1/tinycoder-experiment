import unittest

from textstats import top_words


class HiddenT1(unittest.TestCase):
    def test_example(self):
        self.assertEqual(top_words("pear apple pear apple fig", 3), [("apple", 2), ("pear", 2), ("fig", 1)])

    def test_all_ties(self):
        self.assertEqual(top_words("d c b a", 4), [("a", 1), ("b", 1), ("c", 1), ("d", 1)])

    def test_count_first(self):
        self.assertEqual(top_words("z z z a a b", 3), [("z", 3), ("a", 2), ("b", 1)])

    def test_more_than_unique(self):
        self.assertEqual(top_words("b a", 10), [("a", 1), ("b", 1)])

    def test_zero_and_default(self):
        self.assertEqual(top_words("a b c d", 0), [])
        self.assertEqual(top_words("d c b a"), [("a", 1), ("b", 1), ("c", 1)])

    def test_original(self):
        self.assertEqual(top_words("b b b a a c", 2), [("b", 3), ("a", 2)])


if __name__ == "__main__":
    unittest.main()
