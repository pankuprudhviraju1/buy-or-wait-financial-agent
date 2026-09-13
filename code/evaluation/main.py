#!/usr/bin/env python3
"""Independent evaluation and contract validation for Buy or Wait?."""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path


COLUMNS = [
    "request_id", "amount_safe_to_pay", "affordability_status",
    "recommended_payment_method", "payment_plan",
    "earliest_date_for_full_payment", "spending_changes_needed",
    "decision_explanation",
]
STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
PAYMENT = re.compile(r"^(\d{4}-\d{2}-\d{2}):([0-9]+(?:\.[0-9]+)?)$")


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != COLUMNS and path.name not in {"requests.csv", "sample_requests.csv"}:
            raise AssertionError(f"Wrong columns in {path}: {reader.fieldnames}")
        return list(reader)


def parse_plan(text: str) -> list[tuple[date, Decimal]]:
    if text == "none":
        return []
    result = []
    for item in text.split("|"):
        match = PAYMENT.fullmatch(item)
        if not match:
            raise AssertionError(f"Malformed payment entry: {item}")
        result.append((date.fromisoformat(match.group(1)), Decimal(match.group(2))))
    if result != sorted(result):
        raise AssertionError("Payment plan is not chronological")
    return result


def validate(dataset: Path, requests_path: Path, output_path: Path) -> list[dict[str, str]]:
    requests = {r["request_id"]: r for r in rows(requests_path)}
    output = rows(output_path)
    if len(output) != len(requests) or {r["request_id"] for r in output} != set(requests):
        raise AssertionError("Output must contain exactly one row for every request")

    profiles = {r["user_id"]: r for r in csv.DictReader((dataset / "financial_profiles.csv").open())}
    events = {r["event_id"]: r for r in csv.DictReader((dataset / "financial_events.csv").open())}
    options = defaultdict(list)
    for option in csv.DictReader((dataset / "request_payment_options.csv").open()):
        options[option["request_id"]].append(option)

    for prediction in output:
        request = requests[prediction["request_id"]]
        profile = profiles[request["user_id"]]
        safe = Decimal(prediction["amount_safe_to_pay"])
        requested = Decimal(request["requested_amount"])
        assert Decimal("0") <= safe <= requested
        assert prediction["affordability_status"] in STATUSES
        assert prediction["recommended_payment_method"] in METHODS
        assert prediction["decision_explanation"].strip()
        plan = parse_plan(prediction["payment_plan"])
        if plan:
            assert plan[-1][0] <= date.fromisoformat(request["desired_completion_date"])
        else:
            assert prediction["recommended_payment_method"] == "not_recommended"

        earliest = prediction["earliest_date_for_full_payment"]
        if earliest:
            date.fromisoformat(earliest)
        if prediction["affordability_status"] == "affordable_now":
            assert earliest == request["request_date"] and safe == requested
        if prediction["recommended_payment_method"] == "wait":
            assert prediction["affordability_status"] == "affordable_later" and len(plan) == 1
        if prediction["recommended_payment_method"] == "partial_payment":
            assert prediction["affordability_status"] == "affordable_with_plan" and len(plan) == 2
            assert plan[0] == (date.fromisoformat(request["request_date"]), safe)
            assert sum((amount for _, amount in plan), Decimal("0")) == requested
        if prediction["recommended_payment_method"] == "installments":
            supplied = {
                tuple(
                    (date.fromisoformat(o["first_payment_date"]) + __import__("datetime").timedelta(
                        days=i * int(o["payment_frequency_days"] or 0)
                    ), Decimal(o["payment_amount"]))
                    for i in range(int(o["number_of_payments"]))
                )
                for o in options[request["request_id"]] if o["payment_method"] == "installments"
            }
            assert tuple(plan) in supplied, f"Installments do not match an option: {request['request_id']}"

        changes = prediction["spending_changes_needed"]
        if changes != "none":
            parts = changes.split("|")
            assert len(parts) <= 3
            seen = set()
            for part in parts:
                fields = part.split(":")
                assert fields[0] in {"stop", "reduce_to"} and len(fields) in {2, 3}
                event = events[fields[1]]
                assert event["user_id"] == request["user_id"] and event["flexibility"] != "fixed"
                assert event["category"] not in profile["expense_categories_to_protect"].split("|")
                assert fields[1] not in seen
                seen.add(fields[1])

    return output


def main() -> None:
    package_root = Path(__file__).resolve().parents[1]
    repository_root = package_root.parent if (package_root.parent / "dataset").is_dir() else package_root
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=repository_root / "dataset")
    parser.add_argument("--mode", choices=["samples", "full"], default="samples")
    parser.add_argument("--output", type=Path, default=repository_root / "output.csv")
    args = parser.parse_args()

    requests_path = args.dataset / ("sample_requests.csv" if args.mode == "samples" else "requests.csv")
    output_path = args.output
    temporary = None
    if args.mode == "samples":
        temporary = tempfile.TemporaryDirectory()
        output_path = Path(temporary.name) / "sample_predictions.csv"
        subprocess.run([
            sys.executable, str(package_root / "main.py"), "--dataset", str(args.dataset),
            "--requests", str(requests_path), "--output", str(output_path),
        ], check=True)

    predictions = validate(args.dataset, requests_path, output_path)
    print(f"Contract validation passed for {len(predictions)} rows.")
    if args.mode == "samples":
        expected = {r["request_id"]: r for r in rows(requests_path)}
        status = sum(r["affordability_status"] == expected[r["request_id"]]["affordability_status"] for r in predictions)
        method = sum(r["recommended_payment_method"] == expected[r["request_id"]]["recommended_payment_method"] for r in predictions)
        exact = sum(
            all(r[column] == expected[r["request_id"]][column] for column in COLUMNS[2:7])
            for r in predictions
        )
        print(f"Public samples: status {status}/{len(predictions)}, method {method}/{len(predictions)}, exact decision {exact}/{len(predictions)}")
        print("Predicted status distribution:", dict(Counter(r["affordability_status"] for r in predictions)))


if __name__ == "__main__":
    main()
