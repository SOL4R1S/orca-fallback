import unittest

from recommend import normalized
from run import is_provider_failure, response_text


class RankingTests(unittest.TestCase):
    def test_lower_cost_and_latency_score_higher(self):
        scores = normalized({"cheap": 1, "expensive": 10}, lower_is_better=True)
        self.assertGreater(scores["cheap"], 0.9)
        self.assertLess(scores["expensive"], 0.1)

    def test_higher_quality_scores_higher(self):
        scores = normalized({"weak": 20, "strong": 80})
        self.assertEqual(scores["weak"], 0)
        self.assertEqual(scores["strong"], 1)


class FailureTests(unittest.TestCase):
    def test_provider_errors_are_distinct_from_task_failures(self):
        self.assertTrue(is_provider_failure("usage limit reached"))
        self.assertTrue(is_provider_failure("invalid_api_key"))
        self.assertFalse(is_provider_failure("AssertionError: expected true"))

    def test_prompt_text_does_not_trigger_failure(self):
        result = {
            "transcript": {
                "messages": [
                    {"role": "user", "blocks": [{"text": "Discuss usage limit handling"}]},
                    {"role": "assistant", "blocks": [{"text": "Done"}]},
                ]
            }
        }
        self.assertFalse(is_provider_failure(response_text(result)))


if __name__ == "__main__":
    unittest.main()
