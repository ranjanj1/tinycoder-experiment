import unittest

from slug import slugify


class SlugTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(slugify("hello"), "hello")

    def test_spaces(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_collapse(self):
        self.assertEqual(slugify("Hello,   World!!"), "hello-world")

    def test_edges(self):
        self.assertEqual(slugify("  --Python 3.12--  "), "python-3-12")

    def test_accents(self):
        self.assertEqual(slugify("Café Déjà Vu"), "cafe-deja-vu")

    def test_truncate_clean(self):
        self.assertEqual(slugify("one two three", max_len=8), "one-two")


if __name__ == "__main__":
    unittest.main()
