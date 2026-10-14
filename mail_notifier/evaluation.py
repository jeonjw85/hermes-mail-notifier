"""Explicit model evaluation against authored cases; no mailbox or delivery access."""

import hashlib
import json
import math
import os
import statistics
import tempfile
import time
from dataclasses import asdict, fields
from pathlib import Path

from .config import MAX_BODY_CHARS, settings_fingerprint
from .message import MailContent
from .policy import CATEGORIES, DECISIONS, POLICY_VERSION
from .service import error_code
from .triage import ModelReply, triage_message

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "mail_cases.jsonl"


def load_cases(path) -> list[dict]:
    with Path(path).open("rb") as source:
        data = source.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("dataset_too_large")
    cases, identifiers = [], set()
    names = {field.name for field in fields(MailContent)}
    try:
        for line in data.decode("utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != {"id", "language", "category", "expected_decision", "critical", "mail", "rationale"}:
                raise ValueError
            if not isinstance(row["id"], str) or not row["id"] or row["id"] in identifiers:
                raise ValueError
            if (row["language"] not in ("ko", "en") or row["category"] not in CATEGORIES
                    or row["expected_decision"] not in DECISIONS or type(row["critical"]) is not bool
                    or not isinstance(row["rationale"], str) or not row["rationale"]):
                raise ValueError
            mail = row["mail"]
            if not isinstance(mail, dict) or set(mail) - names or not {"sender_address", "subject", "text"} <= set(mail):
                raise ValueError
            mail = {"sender_name": "", "date": "", "truncated": False, "needs_body_review": False, **mail}
            for name, value in mail.items():
                if name in ("truncated", "needs_body_review"):
                    if type(value) is not bool:
                        raise ValueError
                elif not isinstance(value, str) or len(value) > (MAX_BODY_CHARS if name == "text" else 420):
                    raise ValueError
            row["mail"] = asdict(MailContent(**mail))
            identifiers.add(row["id"])
            cases.append(row)
        if not cases:
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("invalid_evaluation_dataset") from None
    return cases


def _metrics(cases, records):
    important = [i for i, case in enumerate(cases) if case["expected_decision"] == "notify"]
    promoted = [i for i, case in enumerate(cases) if case["category"] == "promotion"]
    predictions = [record.get("result", {}).get("decision") for record in records]
    notified = [i for i, value in enumerate(predictions) if value == "notify"]
    alerts = sum(value in ("notify", "review") for value in predictions)
    correct = sum(predictions[i] == "notify" for i in important)
    return {"recall": correct / len(important) if important else None,
            "precision": correct / alerts if alerts else None,
            "notify_precision": correct / len(notified) if notified else None,
            "promotion_false_positive": sum(predictions[i] in ("notify", "review") for i in promoted) / len(promoted) if promoted else None,
            "review_rate": predictions.count("review") / len(cases),
            "critical_misses": sum(case["critical"] and predictions[i] != case["expected_decision"] for i, case in enumerate(cases)),
            "errors": sum("error" in record for record in records),
            "incorrect": [case["id"] for i, case in enumerate(cases) if predictions[i] != case["expected_decision"]]}


def evaluate(cases, complete, *, settings, runs=3, monotonic=time.monotonic) -> dict:
    if not cases or type(runs) is not int or not 1 <= runs <= 10:
        raise ValueError("invalid_evaluation_options")
    identities, reports = set(), []
    for run in range(runs):
        records, costs, tokens = [], [], []
        for case in cases:
            started = monotonic()
            record = {"case_id": case["id"]}
            reply = None
            def measured_complete(payload):
                nonlocal reply
                reply = complete(payload)
                return reply
            try:
                result = triage_message(MailContent(**case["mail"]), settings, measured_complete, account_label="Evaluation")
                record["result"] = asdict(result)
            except Exception as exc:
                record["error"] = error_code(exc)
            # Attribution and consumption belong to the call, even if its output is invalid.
            usage = reply.usage if isinstance(reply, ModelReply) else {}
            identity = (reply.provider, reply.model) if isinstance(reply, ModelReply) else ("", "")
            identity = tuple(value.strip() if isinstance(value, str) else "" for value in identity)
            identities.add(identity)
            record.update(usage=usage, provider=identity[0], model=identity[1])
            costs.append(usage.get("cost_usd"))
            tokens.append(usage.get("total_tokens"))
            record["latency_seconds"] = max(0, monotonic() - started)
            records.append(record)
        metrics = _metrics(cases, records)
        delays = sorted(row["latency_seconds"] for row in records)
        passed = (metrics["recall"] is not None and metrics["recall"] >= .95
                  and metrics["promotion_false_positive"] is not None and metrics["promotion_false_positive"] <= .05
                  and metrics["critical_misses"] == 0 and metrics["errors"] == 0)
        reports.append({"run": run + 1, "metrics": metrics, "thresholds_pass": passed,
                        "latency": {"total_seconds": sum(delays), "p50_seconds": statistics.median(delays),
                                    "p95_seconds": delays[math.ceil(len(delays) * .95) - 1]},
                        "usage": {"total_tokens": sum(value for value in tokens if value is not None),
                                  "unknown_token_records": tokens.count(None),
                                  "cost_usd": sum(costs) if all(value is not None for value in costs) else None},
                        "cases": records})
    coverage = ({case["category"] for case in cases} == set(CATEGORIES)
                and {case["language"] for case in cases} == {"ko", "en"}
                and any(case["critical"] for case in cases))
    unresolved = {"", "auto", "default", "unknown"}
    identity_known = len(identities) == 1 and all(value.casefold() not in unresolved for identity in identities for value in identity)
    dataset_hash = hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"policy_version": POLICY_VERSION, "settings_fingerprint": settings_fingerprint(settings),
            "dataset_sha256": dataset_hash, "case_count": len(cases), "models": sorted(identities),
            "runs": reports, "release_pass": runs >= 3 and len(cases) >= 120 and coverage and identity_known
            and all(run["thresholds_pass"] for run in reports)}


def write_report(path, report) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = file.name
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
