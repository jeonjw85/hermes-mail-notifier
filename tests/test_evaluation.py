import json
import argparse
import io
import tempfile
import unittest
from dataclasses import asdict
from contextlib import redirect_stdout
from pathlib import Path

from mail_notifier.config import Settings
from mail_notifier.message import MailContent
from mail_notifier.triage import ModelReply
from tests.support import classification_data, feature


def case(identifier, category="work", decision="notify", critical=False):
    return {"id": identifier, "language": "en", "category": category, "expected_decision": decision,
            "critical": critical, "rationale": "direct request", "mail": asdict(MailContent(
                "person@example.com", "Person", identifier, "", "Please reply.", False, False))}


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.evaluation")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_review_and_errors_remain_in_metric_denominators(self):
        cases = [case("w1"), case("w2", critical=True), case("p1", "promotion", "suppress"), case("p2", "promotion", "suppress")]
        replies = iter(["notify", "review", "notify", "suppress"])
        report = self.module.evaluate(cases, lambda _: ModelReply(classification_data(decision=next(replies)), {}, "test", "one"), settings=Settings(), runs=1)
        metrics = report["runs"][0]["metrics"]
        for key, value in {"recall": .5, "precision": 1 / 3, "notify_precision": .5, "promotion_false_positive": .5,
                           "review_rate": .25, "critical_misses": 1}.items():
            self.assertEqual(metrics[key], value)
        self.assertFalse(report["release_pass"])
        def broken(_):
            raise RuntimeError("private raw provider text")
        failed = self.module.evaluate(cases, broken, settings=Settings(), runs=1)
        self.assertEqual(failed["runs"][0]["metrics"]["recall"], 0)
        self.assertEqual(failed["runs"][0]["metrics"]["errors"], 4)
        self.assertNotIn("private raw", json.dumps(failed))

    def test_release_gate_requires_three_runs_complete_corpus_and_one_known_model(self):
        cases = self.module.load_cases(Path(__file__).resolve().parents[1] / "evaluation/mail_cases.jsonl")
        self.assertGreaterEqual(len(cases), 120)
        self.assertEqual({row["language"] for row in cases}, {"ko", "en"})
        def outputs(model):
            expected = iter(cases * 3)
            def complete(_):
                row = next(expected)
                return ModelReply(classification_data(category=row["category"], decision=row["expected_decision"]),
                                  {"total_tokens": 10, "cost_usd": None}, "test", model(row))
            return complete
        good = self.module.evaluate(cases, outputs(lambda _: "one"), settings=Settings())
        self.assertTrue(good["release_pass"])
        self.assertIsNone(good["runs"][0]["usage"]["cost_usd"])
        self.assertFalse(self.module.evaluate(cases, outputs(lambda row: row["id"]), settings=Settings())["release_pass"])
        self.assertFalse(self.module.evaluate(cases, outputs(lambda _: ""), settings=Settings())["release_pass"])

    def test_host_fallback_attribution_cannot_qualify_a_release(self):
        cases = self.module.load_cases(self.module.DEFAULT_DATASET)
        for provider, model in (("auto", "default"), ("auto", "resolved-model"),
                                ("resolved-provider", "default"), ("resolved-provider", "unknown"),
                                ("resolved-provider", " ")):
            expected = iter(cases * 3)
            def complete(_):
                row = next(expected)
                return ModelReply(classification_data(category=row["category"], decision=row["expected_decision"]),
                                  {}, provider, model)
            with self.subTest(provider=provider, model=model):
                report = self.module.evaluate(cases, complete, settings=Settings())
                self.assertTrue(all(run["thresholds_pass"] for run in report["runs"]))
                self.assertFalse(report["release_pass"])

    def test_review_alerts_reduce_notification_precision(self):
        cases = [case("work"), case("newsletter", "newsletter", "suppress"), case("uncertain", "unknown", "review")]
        replies = iter(["notify", "review", "review"])
        report = self.module.evaluate(cases, lambda _: classification_data(decision=next(replies)), settings=Settings(), runs=1)
        metrics = report["runs"][0]["metrics"]
        self.assertEqual(metrics["precision"], 1 / 3)
        self.assertEqual(metrics["notify_precision"], 1)
        self.assertEqual(metrics["review_rate"], 2 / 3)

    def test_invalid_output_retains_available_usage_without_raw_reply(self):
        reply = ModelReply(classification_data(evidence=["unquoted private model text"]),
                           {"total_tokens": 100, "cost_usd": .01}, "test", "one")
        report = self.module.evaluate([case("invalid")], lambda _: reply, settings=Settings(), runs=1)
        run = report["runs"][0]
        self.assertEqual(run["metrics"]["errors"], 1)
        self.assertEqual(run["usage"], {"total_tokens": 100, "unknown_token_records": 0, "cost_usd": .01})
        self.assertEqual(run["cases"][0]["usage"], reply.usage)
        self.assertNotIn("unquoted private", json.dumps(report))

    def test_invalid_or_oversized_dataset_is_rejected(self):
        path = Path(self.temp.name) / "cases.jsonl"
        for rows in ([case("same"), case("same")], [{**case("a"), "category": "invalid"}],
                     [{**case("a"), "expected_decision": "discard"}], [{**case("a"), "mail": {}}]):
            path.write_text("\n".join(json.dumps(row) for row in rows))
            with self.assertRaises(ValueError):
                self.module.load_cases(path)
        path.write_text("x" * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.module.load_cases(path)

    def test_report_is_written_as_valid_json_and_replaces_atomically(self):
        path = Path(self.temp.name) / "report.json"
        path.write_text("old")
        self.module.write_report(path, {"release_pass": False, "runs": []})
        self.assertEqual(json.loads(path.read_text()), {"release_pass": False, "runs": []})

    def test_smoke_cli_calls_model_without_accessing_mail_or_store(self):
        from mail_notifier.cli import configure_parser, run_cli
        class Host:
            def load_settings(self):
                return Settings()
            def complete_structured(self, payload):
                return ModelReply(classification_data(), {}, "test", "one")
        parser = argparse.ArgumentParser()
        configure_parser(parser)
        path = Path(self.temp.name) / "smoke.json"
        with redirect_stdout(io.StringIO()):
            code = run_cli(parser.parse_args(["evaluate", "--limit", "2", "--runs", "1", "--output", str(path)]), Host())
        self.assertEqual(code, 0)
        report = json.loads(path.read_text())
        self.assertEqual(report["case_count"], 2)
        self.assertFalse(report["release_pass"])
