import unittest

from catalog import comparison_price, policy_exclusions, resolve_policy, role_lane


class LanePolicyTests(unittest.TestCase):
    def test_economy_lane_rejects_expensive_or_low_quality_models(self):
        budget = {"max_comparison_price_usd": 1, "min_intelligence_index": 20}
        cheap = {
            "pricing": {"cost4": {"input": 0.1, "output": 0.4}},
        }
        self.assertLess(comparison_price(cheap), 1)
        self.assertEqual(policy_exclusions(cheap, budget, 28), [])
        self.assertIn("quality_below_20", policy_exclusions(cheap, budget, 12))
        expensive = {"openrouter": {"pricing_per_million": {"input": 2, "output": 8}}}
        self.assertIn("price_above_1", policy_exclusions(expensive, budget, 60))

    def test_missing_evidence_does_not_pass_a_budget(self):
        budget = {"max_comparison_price_usd": 1, "min_intelligence_index": 20}
        self.assertEqual(
            policy_exclusions({}, budget, None), ["price_unknown", "quality_unknown"]
        )

    def test_research_defaults_to_economy_without_escalation(self):
        lane, config = role_lane("research")
        self.assertEqual(lane, "economy")
        self.assertFalse(config["allow_lane_escalation"])


if __name__ == "__main__":
    unittest.main()
