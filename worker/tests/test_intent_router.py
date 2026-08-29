import unittest
from pathlib import Path

import XianyuAgent


ROOT = Path(__file__).resolve().parents[1]


class InternalReplyClientConvergenceTests(unittest.TestCase):
    def test_worker_agent_has_no_classifier_or_multi_agent_exports(self):
        for name in (
            "IntentRouter",
            "ClassifyAgent",
            "PriceAgent",
            "TechAgent",
            "DefaultAgent",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(XianyuAgent, name))

    def test_worker_agent_does_not_read_prompt_files(self):
        source = (ROOT / "XianyuAgent.py").read_text(encoding="utf-8")
        self.assertNotIn("prompts", source)
        self.assertNotIn("chat.completions", source)
        self.assertIn("/ai/reply", source)


if __name__ == "__main__":
    unittest.main()
