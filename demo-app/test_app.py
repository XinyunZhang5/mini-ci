import unittest

from app import greeting


class GreetingTest(unittest.TestCase):
    def test_greeting_mentions_mini_ci(self):
        self.assertIn("mini-ci", greeting())


if __name__ == "__main__":
    unittest.main()
