import unittest

from tests.support import feature


class LoadScenarioTests(unittest.TestCase):
    def test_bounded_restart_scenario_completes_every_account(self):
        module = feature(self, "tests.load_scenario")
        report = module.simulate(accounts=5, messages=25)
        self.assertTrue(report["success"], report)
        self.assertEqual(report["unique_deliveries"], 125)
        self.assertEqual(report["unfinished"], 0)
        self.assertLessEqual(report["peak_pending"], 2000)
        self.assertLessEqual(report["peak_cached"], 200)
        self.assertEqual(report["completed_accounts"], 5)
        self.assertGreater(report["runs"], 2)
