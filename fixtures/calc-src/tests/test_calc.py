import unittest

from calc import add, mul


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-4, 10), 6)
        self.assertEqual(add(0, 0), 0)

    def test_mul(self):
        self.assertEqual(mul(3, 4), 12)


if __name__ == "__main__":
    unittest.main()
