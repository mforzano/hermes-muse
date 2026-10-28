import re
import unittest
from pathlib import Path

PROMPT = (Path(__file__).resolve().parents[1] / "prompts/system.md").read_text(encoding="utf-8")


class SystemPromptTest(unittest.TestCase):
    def test_fits_registered_section_limit(self):
        self.assertLessEqual(len(PROMPT), 4000)  # register_system_prompt_section(max_chars=4000)

    def test_delegation_guidance_is_generic(self):
        self.assertIn("one or two steps", PROMPT)
        self.assertIn("asynchronously", PROMPT)
        self.assertNotIn("short work", PROMPT)
        self.assertIn("Dispatch quietly", PROMPT)
        self.assertIsNone(re.search(r"delegate_task|paseo|claude|gpt|/Users/", PROMPT, re.I))


if __name__ == "__main__":
    unittest.main()
