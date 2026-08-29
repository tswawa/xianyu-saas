import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = ROOT / "prompts"


def read_prompt(name):
    path = PROMPT_DIR / name
    if path.is_file():
        return path.read_text(encoding="utf-8")
    example = PROMPT_DIR / name.replace("_prompt.txt", "_prompt_example.txt")
    return example.read_text(encoding="utf-8")


class PromptContentTests(unittest.TestCase):
    def test_live_prompts_match_compatibility_templates(self):
        for name in ("classify", "default", "price", "tech"):
            with self.subTest(name=name):
                self.assertEqual(
                    read_prompt(f"{name}_prompt.txt"),
                    read_prompt(f"{name}_prompt_example.txt"),
                )

    def test_compatibility_prompts_contain_no_product_specific_facts_or_magic_tokens(self):
        forbidden = (
            "DeepSeek",
            "deepseek-",
            "元/百万 token",
            "[TRIAL]",
            "[TUTORIAL]",
            "3 折",
            "5 折",
        )
        for path in PROMPT_DIR.glob("*_prompt_example.txt"):
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("不参与 Worker 运行时回复", content)
                for value in forbidden:
                    self.assertNotIn(value, content)

    def test_runtime_client_does_not_reference_prompt_directory(self):
        source = (ROOT / "XianyuAgent.py").read_text(encoding="utf-8")
        self.assertNotIn("prompt_dir", source)
        self.assertNotIn("_init_system_prompts", source)


if __name__ == "__main__":
    unittest.main()
