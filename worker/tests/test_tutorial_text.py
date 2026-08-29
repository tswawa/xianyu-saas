import unittest

from tutorial_text import TUTORIAL_TEXT


class TutorialTextTests(unittest.TestCase):
    def test_deepseek_flash_and_pro_models_and_discounts_are_documented(self):
        self.assertIn("deepseek-v4-flash", TUTORIAL_TEXT)
        self.assertIn("deepseek-v4-pro", TUTORIAL_TEXT)
        self.assertIn("Flash 官方价格 3 折", TUTORIAL_TEXT)
        self.assertIn("Pro 官方价格 5 折", TUTORIAL_TEXT)
        self.assertNotIn("Pro 官方价格 8 折", TUTORIAL_TEXT)
        self.assertIn("同一个 DeepSeek key", TUTORIAL_TEXT)

    def test_exact_buyer_prices_are_documented_as_per_million_token(self):
        self.assertIn("计费标准（元/百万 token，以下均为实付单价）", TUTORIAL_TEXT)
        self.assertIn("Flash 3 折：输入 0.3，输出 0.6，缓存命中 0.006", TUTORIAL_TEXT)
        self.assertIn("Pro 5 折：输入 1.5，输出 3.0，缓存命中 0.0125", TUTORIAL_TEXT)

    def test_transient_deepseek_issues_have_bounded_retry_guidance(self):
        self.assertIn("DeepSeek 偶发网络异常、超时或卡顿", TUTORIAL_TEXT)
        self.assertIn("稍后重试 1-2 次", TUTORIAL_TEXT)


if __name__ == "__main__":
    unittest.main()
