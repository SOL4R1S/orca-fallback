import tempfile
import unittest
from pathlib import Path

from openrouter import match_models
from run import cooldown_remaining, update_cooldown


class MatchingTests(unittest.TestCase):
    def test_exact_model_matches_and_no_fuzzy_version_match(self):
        local = [
            {"key": "codex:gpt-6.2-sol", "agent": "codex", "provider": "openai", "model": "gpt-6.2-sol"},
            {"key": "codex:gpt-6.2-sol-pro", "agent": "codex", "provider": "openai", "model": "gpt-6.2-sol-pro"},
            {"key": "agy:hydrogen", "agent": "antigravity", "provider": "antigravity", "model": "gemini-4-hydrogen-high"},
            {"key": "codex:alibaba/deepseek", "agent": "codex", "provider": "alibaba-token-plan-sg", "model": "alibaba-token-plan-sg/deepseek-v4.1-flash-1017"},
        ]
        remote = [
            {"id": "openai/gpt-6.2-sol"},
            {"id": "google/gemini-4-hydrogen"},
            {"id": "deepseek/deepseek-v4.1-flash-1017"},
        ]
        matches, unmatched = match_models(local, remote)
        self.assertIn("codex:gpt-6.2-sol", matches)
        self.assertIn("agy:hydrogen", matches)
        self.assertIn("codex:alibaba/deepseek", matches)
        self.assertIn("codex:gpt-6.2-sol-pro", unmatched)


class CooldownTests(unittest.TestCase):
    def test_cooldown_expires_and_clears(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cooldowns.json"
            update_cooldown(path, "model", 60)
            self.assertGreater(cooldown_remaining(path, "model"), 0)
            update_cooldown(path, "model", 0)
            self.assertEqual(cooldown_remaining(path, "model"), 0)


if __name__ == "__main__":
    unittest.main()
