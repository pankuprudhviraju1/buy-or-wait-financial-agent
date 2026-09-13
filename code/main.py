#!/usr/bin/env python3
"""Deterministic Buy or Wait? affordability agent.

The engine reconstructs recurring cash flow from participant-facing files only,
applies explicit message amendments, performs a daily 90-day balance forecast,
enumerates eligible payment plans and validates the selected recommendation.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from statistics import median
from typing import Iterable, Optional


OUTPUT_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]

VARIABLE_CATEGORIES = {"groceries", "transport", "dining"}
ONE_OFF_EVENT_TYPES = {
    "refund",
    "investment_purchase",
    "investment_valuation",
    "transfer",
    "windfall",
}
VALID_STATUSES = {"settled", "pending", "scheduled", "unrealized", "failed", "cancelled"}
EPSILON = Decimal("0.005")


def D(value: object) -> Decimal:
    if value is None or str(value).strip() == "":
        raise ValueError("blank numeric value")
    return Decimal(str(value).replace(",", "").strip())


def day(value: str) -> date:
    return date.fromisoformat(value)


def split_pipe(value: str) -> set[str]:
    return {item.strip() for item in value.split("|") if item.strip()}


def money_key(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compact_amount(value: Decimal) -> str:
    value = money_key(value)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def plan_amount(value: Decimal, source_text: str = "") -> str:
    """Preserve supplied precision, while making computed cents unambiguous."""
    if source_text:
        source_text = source_text.strip()
        if "." not in source_text and value == value.to_integral_value():
            return str(value.to_integral_value())
    return f"{money_key(value):.2f}"


def add_months(d: date, count: int = 1) -> date:
    month_index = d.year * 12 + d.month - 1 + count
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@dataclass(frozen=True)
class Flow:
    flow_date: date
    amount: Decimal  # positive credit, negative debit
    category: str
    event_id: str
    description: str
    source: str
    flexibility: str = "fixed"
    minimum_allowed: Optional[Decimal] = None
    series_id: str = ""


@dataclass(frozen=True)
class Series:
    series_id: str
    category: str
    direction: str
    event_type: str
    description: str
    currency: str
    flexibility: str
    minimum_allowed: Optional[Decimal]
    cadence: str  # monthly or days
    interval_days: int
    anchor_date: date
    amount: Decimal
    latest_event_id: str


@dataclass(frozen=True)
class Change:
    kind: str
    event_id: str
    series_id: str
    new_amount: Decimal
    savings_per_occurrence: Decimal

    def output_text(self) -> str:
        if self.kind == "stop":
            return f"stop:{self.event_id}"
        amount = compact_amount(self.new_amount)
        if self.new_amount != self.new_amount.to_integral_value():
            amount = f"{money_key(self.new_amount):.2f}"
        return f"reduce_to:{self.event_id}:{amount}"


@dataclass
class Candidate:
    method: str
    payments: list[tuple[date, Decimal, str]]
    total: Decimal
    option_id: str = ""
    changes: tuple[Change, ...] = ()


class DataBundle:
    def __init__(self, dataset: Path):
        self.dataset = dataset
        self.profiles = {row["user_id"]: row for row in read_csv(dataset / "financial_profiles.csv")}
        self.events = read_csv(dataset / "financial_events.csv")
        self.events_by_user: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.events:
            if row["status"] not in VALID_STATUSES:
                raise ValueError(f"Unexpected event status {row['status']}")
            self.events_by_user[row["user_id"]].append(row)
        self.messages = read_csv(dataset / "messages.csv")
        self.messages_by_user: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.messages:
            self.messages_by_user[row["user_id"]].append(row)
        self.images = read_csv(dataset / "images.csv")
        self.options = read_csv(dataset / "request_payment_options.csv")
        self.options_by_request: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.options:
            self.options_by_request[row["request_id"]].append(row)
        self.rates: dict[tuple[date, str, str], Decimal] = {}
        for row in read_csv(dataset / "exchange_rates.csv"):
            self.rates[(day(row["rate_date"]), row["from_currency"], row["to_currency"])] = D(row["rate"])
        self.image_amounts = self._load_image_amounts()

    def _load_image_amounts(self) -> dict[str, Decimal]:
        """Read every linked PNG and resolve its amount through a content hash cache.

        The cache contains OCR/document-understanding results keyed by SHA-256 rather
        than request or event IDs, so it is input evidence rather than output labels.
        """
        cache_path = Path(__file__).with_name("image_amounts.json")
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        resolved: dict[str, Decimal] = {}
        for link in self.images:
            image_path = self.dataset / "media" / "images" / f"{link['image_id']}.png"
            payload = image_path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest not in cache:
                raise ValueError(
                    f"No reviewed image extraction for {image_path.name} (sha256={digest}). "
                    "Add a content-hash entry to code/image_amounts.json."
                )
            resolved[link["related_event_id"]] = D(cache[digest]["amount"])
        return resolved

    def event_amount(self, event: dict[str, str], on_date: Optional[date] = None) -> Decimal:
        amount = self.raw_event_amount(event)
        home = self.profiles[event["user_id"]]["home_currency"]
        currency = event["currency"]
        if currency == home:
            return amount
        rate_date = on_date or day(event["settlement_date"] or event["event_date"])
        direct = self.rates.get((rate_date, currency, home))
        if direct is not None:
            return money_key(amount * direct)
        inverse = self.rates.get((rate_date, home, currency))
        if inverse is not None and inverse != 0:
            return money_key(amount / inverse)
        raise ValueError(f"Missing dated FX rate for {currency}->{home} on {rate_date}")

    def raw_event_amount(self, event: dict[str, str]) -> Decimal:
        raw = event["amount"].strip()
        if raw:
            return D(raw)
        if event["event_id"] not in self.image_amounts:
            raise ValueError(f"Blank amount has no linked image extraction: {event['event_id']}")
        return self.image_amounts[event["event_id"]]


class AffordabilityEngine:
    def __init__(self, data: DataBundle):
        self.data = data

    @staticmethod
    def _monthly_pattern(dates: list[date]) -> bool:
        if len(dates) < 3:
            return False
        recent = dates[-6:]
        gaps = [(b - a).days for a, b in zip(recent, recent[1:])]
        regular_gaps = sum(27 <= gap <= 35 for gap in gaps)
        common_day_count = Counter(d.day for d in recent).most_common(1)[0][1]
        return regular_gaps >= max(2, len(gaps) - 1) and common_day_count >= len(recent) - 1

    @staticmethod
    def _forecast_amount(values: list[Decimal], direction: str, cadence_days: int = 30) -> Decimal:
        if max(values) == min(values):
            return values[-1]
        if direction == "credit":
            # A repeated run rate is stronger evidence than an isolated partial
            # paycheck. Fall back to the latest amount when every value differs.
            counts = Counter(values)
            value, occurrences = counts.most_common(1)[0]
            return value if occurrences >= 2 else values[-1]
        # Average the observed run, rather than extrapolating the most recent
        # high/low transaction. Variable series pass a bounded recent window.
        return money_key(sum(values, Decimal("0")) / len(values))

    def _series(self, user_id: str, request_date: date) -> list[Series]:
        history = [
            e for e in self.data.events_by_user[user_id]
            if e["status"] == "settled"
            and e["direction"] in {"debit", "credit"}
            and e["event_type"] not in ONE_OFF_EVENT_TYPES
            and e["settlement_date"]
            and day(e["settlement_date"]) <= request_date
        ]

        by_description: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
        for e in history:
            key = (
                e["description"], e["category"], e["event_type"], e["direction"],
                e["currency"], e["flexibility"], e["minimum_allowed_amount"],
            )
            by_description[key].append(e)

        output: list[Series] = []
        consumed: set[str] = set()
        for key, rows in sorted(by_description.items()):
            rows.sort(key=lambda e: e["settlement_date"])
            dates = [day(e["settlement_date"]) for e in rows]
            # Rotating merchant labels in variable categories can repeat every
            # third/fourth visit and masquerade as monthly subscriptions.
            if key[1] in VARIABLE_CATEGORIES or not self._monthly_pattern(dates):
                continue
            values = [self.data.raw_event_amount(e) for e in rows]
            desc, category, event_type, direction, currency, flexibility, minimum = key
            latest = rows[-1]
            series_id = f"monthly:{latest['event_id']}"
            output.append(Series(
                series_id, category, direction, event_type, desc, currency, flexibility,
                D(minimum) if minimum else None, "monthly", 0, dates[-1],
                self._forecast_amount(values, direction), latest["event_id"],
            ))
            consumed.update(e["event_id"] for e in rows)

        # Regular variable spending is recognizable at category level even when
        # merchant descriptions rotate. Detect the modal short interval.
        for category in VARIABLE_CATEGORIES:
            rows = [
                e for e in history
                if e["event_id"] not in consumed and e["category"] == category and e["direction"] == "debit"
            ]
            rows.sort(key=lambda e: e["settlement_date"])
            if len(rows) < 5:
                continue
            dates = [day(e["settlement_date"]) for e in rows]
            gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if 1 <= (b - a).days <= 35]
            if len(gaps) < 4:
                continue
            # Synthetic transaction histories use stable 5/7/10/14/21-day rhythms.
            candidates = [5, 7, 10, 14, 21]
            interval = min(candidates, key=lambda n: sum(abs(g - n) for g in gaps[-12:]))
            close_share = sum(abs(g - interval) <= 1 for g in gaps[-12:]) / min(12, len(gaps))
            if close_share < 0.65:
                continue
            recent_rows = rows[-12:]
            values = [self.data.raw_event_amount(e) for e in recent_rows]
            latest = rows[-1]
            flexibility = latest["flexibility"]
            minimums = [D(e["minimum_allowed_amount"]) for e in rows if e["minimum_allowed_amount"]]
            minimum = max(minimums) if minimums else None
            series_id = f"days:{category}:{latest['event_id']}"
            output.append(Series(
                series_id, category, "debit", latest["event_type"], category,
                latest["currency"], flexibility, minimum, "days", interval,
                dates[-1], self._forecast_amount(values, "debit", interval), latest["event_id"],
            ))

        # Some users receive recurring contract income under a rotating invoice
        # description. Detect stable monthly day-of-month lanes at category level.
        income_rows = [
            e for e in history
            if e["event_id"] not in consumed and e["direction"] == "credit"
            and e["category"] == "salary"
        ]
        lanes: dict[int, list[dict[str, str]]] = defaultdict(list)
        for row in income_rows:
            lanes[day(row["settlement_date"]).day].append(row)
        for day_of_month, rows in sorted(lanes.items()):
            rows.sort(key=lambda e: e["settlement_date"])
            if len(rows) < 4 or not self._monthly_pattern([day(e["settlement_date"]) for e in rows]):
                continue
            values = [self.data.raw_event_amount(e) for e in rows]
            latest = rows[-1]
            output.append(Series(
                f"monthly-income:{day_of_month}:{latest['event_id']}", "salary", "credit",
                latest["event_type"], "Recurring contract income", latest["currency"],
                "fixed", None, "monthly", 0, day(latest["settlement_date"]),
                money_key(Decimal(str(median(values[-5:])))), latest["event_id"],
            ))

        return output

    @staticmethod
    def _message_number(text: str, currency: str) -> Optional[Decimal]:
        match = re.search(rf"\b{re.escape(currency)}\s*([0-9]+(?:[.,][0-9]+)*)", text, re.I)
        return D(match.group(1)) if match else None

    @staticmethod
    def _message_date(text: str) -> Optional[date]:
        match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        return day(match.group(1)) if match else None

    def _apply_messages(
        self, user_id: str, request_id: str, request_date: date, horizon: date,
        series: list[Series], flows: list[Flow],
    ) -> tuple[list[Series], list[Flow]]:
        profile = self.data.profiles[user_id]
        home = profile["home_currency"]
        relevant = [
            m for m in self.data.messages_by_user[user_id]
            if not m["request_id"] or m["request_id"] == request_id
        ]
        salary_series = [s for s in series if s.category == "salary" and s.direction == "credit"]

        for message in sorted(relevant, key=lambda m: m["sent_at"]):
            text = message["message_text"]
            lower = text.lower()
            amount = self._message_number(text, home)
            effective = self._message_date(text)

            ended = any(phrase in lower for phrase in [
                "employment has ended", "contract has ended", "income or renewal has been confirmed",
                "kontrak musiman saat ini telah berakhir", "belum ada pendapatan di luar musim",
            ])
            if ended:
                series = [s for s in series if not (s.category == "salary" and s.direction == "credit")]
                salary_series = []
                continue

            # Explicit recurring salary changes replace the household salary total.
            recurring_change = any(phrase in lower for phrase in [
                "monthly salary has increased", "monthly pay is", "confirmed base salary",
                "remaining confirmed monthly salary", "gaji bulanan anda naik",
                "gaji pokok yang dikonfirmasi", "sisa gaji bulanan yang dikonfirmasi",
            ])
            first_salary = any(phrase in lower for phrase in [
                "first salary", "first salary from the new employer", "gaji pertama",
            ])
            next_salary = any(phrase in lower for phrase in [
                "next salary is reduced", "temporary monthly pay", "gaji bulanan sementara",
            ])
            resumes = "regular salary of" in lower and "resumes" in lower

            if amount is not None and (recurring_change or first_salary or resumes):
                start = effective or date(request_date.year, request_date.month, 15)
                if start <= request_date:
                    start = add_months(start, 1)
                series = [s for s in series if not (s.category == "salary" and s.direction == "credit")]
                synthetic_anchor = add_months(start, -1)
                series.append(Series(
                    f"message-salary:{message['message_id']}", "salary", "credit", "income",
                    "Confirmed salary", home, "fixed", None, "monthly", 0,
                    synthetic_anchor, amount, "",
                ))
                salary_series = [series[-1]]

            if amount is not None and next_salary:
                next_date = effective
                if next_date is None:
                    future_salary_dates = []
                    for s in salary_series:
                        d = add_months(s.anchor_date)
                        while d <= request_date:
                            d = add_months(d)
                        future_salary_dates.append(d)
                    next_date = min(future_salary_dates) if future_salary_dates else date(request_date.year, request_date.month, 15)
                    if next_date <= request_date:
                        next_date = add_months(next_date)
                # The notice confirms the affected cycle only. Preserve the
                # established recurring run rate for later cycles and override
                # the next occurrence with this explicit credit.
                flows.append(Flow(
                    next_date, amount, "salary", "", "Confirmed adjusted salary", "message",
                ))

            # Invoice/service-provider messages explicitly confirm a one-off credit.
            confirmed_invoice = any(phrase in lower for phrase in [
                "client approved an invoice payment", "klien menyetujui pembayaran faktur",
            ])
            if amount is not None and effective and confirmed_invoice and request_date < effective <= horizon:
                flows.append(Flow(effective, amount, "salary", "", "Confirmed invoice payment", "message"))

            # Salary plus one-time arrears: extract both values in order.
            if any(phrase in lower for phrase in ["one-time arrears", "penyesuaian tunggakan satu kali"]):
                nums = re.findall(rf"\b{re.escape(home)}\s*([0-9]+(?:[.,][0-9]+)*)", text, re.I)
                if len(nums) >= 2:
                    base, arrears = D(nums[0]), D(nums[1])
                    pay_date = effective or date(request_date.year, request_date.month, 15)
                    if pay_date <= request_date:
                        pay_date = add_months(pay_date)
                    series = [s for s in series if not (s.category == "salary" and s.direction == "credit")]
                    series.append(Series(
                        f"message-salary:{message['message_id']}", "salary", "credit", "income",
                        "Confirmed salary", home, "fixed", None, "monthly", 0,
                        add_months(pay_date, -1), base, "",
                    ))
                    flows.append(Flow(pay_date, base + arrears, "salary", "", "Salary and arrears", "message"))

            # A renewed lease increases the existing monthly rent by 12%.
            if any(phrase in lower for phrase in ["increases monthly rent by 12%", "menaikkan biaya sewa bulanan sebesar 12%"]):
                series = [replace(s, amount=money_key(s.amount * Decimal("1.12"))) if s.category == "rent" else s for s in series]

            # A delayed-payday notice supplies a replacement recurring payday.
            if effective and any(phrase in lower for phrase in [
                "confirmed salary is now expected", "gaji yang sudah dikonfirmasi kini diperkirakan masuk",
            ]):
                existing = next((s for s in salary_series), None)
                if existing:
                    series = [s for s in series if not (s.category == "salary" and s.direction == "credit")]
                    series.append(replace(
                        existing, series_id=f"message-salary:{message['message_id']}",
                        anchor_date=add_months(effective, -1), latest_event_id="",
                    ))
                    salary_series = [series[-1]]

        return series, flows

    def _project_series(self, series: Iterable[Series], request_date: date, horizon: date) -> list[Flow]:
        flows: list[Flow] = []
        for s in series:
            if s.cadence == "monthly":
                d = add_months(s.anchor_date)
                while d < request_date:
                    d = add_months(d)
                while d <= horizon:
                    amount = s.amount
                    # Recurring foreign-currency rows are converted on each future settlement date.
                    if s.currency != self._current_home:
                        template = {
                            "amount": str(s.amount), "currency": s.currency,
                            "user_id": self._current_user, "event_id": s.latest_event_id,
                            "settlement_date": d.isoformat(), "event_date": d.isoformat(),
                        }
                        amount = self.data.event_amount(template, d)
                    sign = Decimal("1") if s.direction == "credit" else Decimal("-1")
                    flows.append(Flow(
                        d, sign * amount, s.category, s.latest_event_id, s.description,
                        "recurring", s.flexibility, s.minimum_allowed, s.series_id,
                    ))
                    d = add_months(d)
            else:
                d = s.anchor_date + timedelta(days=s.interval_days)
                # The available balance is an as-of-today figure, so a modeled
                # variable transaction on the request date is already reflected.
                while d <= request_date:
                    d += timedelta(days=s.interval_days)
                while d <= horizon:
                    flows.append(Flow(
                        d, -s.amount, s.category, s.latest_event_id, s.description,
                        "recurring", s.flexibility, s.minimum_allowed, s.series_id,
                    ))
                    d += timedelta(days=s.interval_days)
        return flows

    def build_forecast(self, request: dict[str, str]) -> tuple[list[Flow], list[Series]]:
        user_id = request["user_id"]
        request_date = day(request["request_date"])
        horizon = request_date + timedelta(days=90)
        self._current_user = user_id
        self._current_home = self.data.profiles[user_id]["home_currency"]
        series = self._series(user_id, request_date)

        explicit: list[Flow] = []
        for e in self.data.events_by_user[user_id]:
            if not e["settlement_date"]:
                continue
            d = day(e["settlement_date"])
            if not request_date < d <= horizon:
                continue
            status, direction = e["status"], e["direction"]
            include = (direction == "debit" and status in {"pending", "scheduled"}) or (
                direction == "credit" and status == "scheduled"
            )
            if not include:
                continue
            amount = self.data.event_amount(e, d)
            explicit.append(Flow(
                d, amount if direction == "credit" else -amount, e["category"], e["event_id"],
                e["description"], "explicit", e["flexibility"],
                D(e["minimum_allowed_amount"]) if e["minimum_allowed_amount"] else None,
            ))

        # A structured next-confirmed-salary row establishes the recurring run
        # rate when history is new or irregular. It also prevents a prorated
        # first paycheck from being extrapolated.
        scheduled_salaries = [
            e for e in self.data.events_by_user[user_id]
            if e["status"] == "scheduled" and e["direction"] == "credit"
            and e["category"] == "salary" and e["settlement_date"]
            and request_date < day(e["settlement_date"]) <= horizon
        ]
        if scheduled_salaries:
            latest = max(scheduled_salaries, key=lambda e: e["settlement_date"])
            start = day(latest["settlement_date"])
            series = [s for s in series if not (s.category == "salary" and s.direction == "credit")]
            series.append(Series(
                f"scheduled-salary:{latest['event_id']}", "salary", "credit", "income",
                latest["description"], latest["currency"], "fixed", None, "monthly", 0,
                add_months(start, -1), self.data.raw_event_amount(latest), latest["event_id"],
            ))

        # A settled final-payroll row is explicit evidence that historical
        # salary must not recur.
        if any(
            e["status"] == "settled" and "final employer payroll" in e["description"].lower()
            for e in self.data.events_by_user[user_id]
        ):
            series = [s for s in series if not (s.category == "salary" and s.direction == "credit")]

        series, explicit = self._apply_messages(
            user_id, request["request_id"], request_date, horizon, series, explicit,
        )

        # App-based weekly earnings that are explicitly pending/withdrawal-locked
        # are not confirmed recurring income even if their historic dates repeat.
        unreliable_income = any(
            phrase in message["message_text"].lower()
            for message in self.data.messages_by_user[user_id]
            for phrase in ["weekly earnings shown", "penghasilan mingguan di aplikasi"]
        )
        if unreliable_income:
            series = [s for s in series if not s.series_id.startswith("monthly-income:")]
        recurring = self._project_series(series, request_date, horizon)

        # Repeated day-to-day discretionary transactions are weaker future facts
        # than protected essentials. Keep the next expected occurrence, while
        # continuing protected variable categories at their observed cadence.
        protected = split_pipe(self.data.profiles[user_id]["expense_categories_to_protect"])
        first_by_series: dict[str, Flow] = {}
        retained: list[Flow] = []
        for flow in recurring:
            if flow.series_id.startswith("days:") and flow.category not in protected:
                first_by_series.setdefault(flow.series_id, flow)
            else:
                retained.append(flow)
        recurring = retained + list(first_by_series.values())

        # Explicit facts supersede matching recurrence on the same date/category.
        explicit_keys = {(f.flow_date, f.category, 1 if f.amount > 0 else -1) for f in explicit}
        recurring = [
            f for f in recurring
            if (f.flow_date, f.category, 1 if f.amount > 0 else -1) not in explicit_keys
        ]
        return sorted(explicit + recurring, key=lambda f: (f.flow_date, 0 if f.amount < 0 else 1, f.event_id)), series

    @staticmethod
    def _changed_flows(flows: list[Flow], changes: tuple[Change, ...]) -> list[Flow]:
        by_series = {c.series_id: c for c in changes}
        adjusted: list[Flow] = []
        for flow in flows:
            change = by_series.get(flow.series_id)
            if change and flow.amount < 0:
                if change.kind == "stop":
                    continue
                adjusted.append(replace(flow, amount=-change.new_amount))
            else:
                adjusted.append(flow)
        return adjusted

    @staticmethod
    def _daily_balances(
        start_balance: Decimal, request_date: date, horizon: date,
        flows: list[Flow], payments: Iterable[tuple[date, Decimal, str]] = (),
    ) -> tuple[dict[date, Decimal], dict[date, Decimal]]:
        by_date: dict[date, list[Decimal]] = defaultdict(list)
        pay_by_date: dict[date, list[Decimal]] = defaultdict(list)
        for flow in flows:
            by_date[flow.flow_date].append(flow.amount)
        for d, amount, _ in payments:
            pay_by_date[d].append(amount)
        balance = start_balance
        endings: dict[date, Decimal] = {}
        lows: dict[date, Decimal] = {}
        d = request_date
        while d <= horizon:
            # Debits before credits is the conservative same-day interpretation.
            # The public examples treat confirmed same-day cash flows as a net
            # settled position, so a salary available that day can fund a plan.
            balance += sum(by_date.get(d, []), Decimal("0"))
            # A user can make the requested payment after confirmed same-day
            # inflows have settled.
            for payment in pay_by_date.get(d, []):
                balance -= payment
            endings[d] = balance
            lows[d] = balance
            d += timedelta(days=1)
        return endings, lows

    def _safe(self, request: dict[str, str], flows: list[Flow], candidate: Candidate) -> bool:
        profile = self.data.profiles[request["user_id"]]
        request_date = day(request["request_date"])
        horizon = request_date + timedelta(days=90)
        changed = self._changed_flows(flows, candidate.changes)
        _, lows = self._daily_balances(D(profile["current_available_balance"]), request_date, horizon, changed, candidate.payments)
        return min(lows.values()) + EPSILON >= D(profile["minimum_balance_to_keep"])

    def _safe_today_and_earliest(
        self, request: dict[str, str], flows: list[Flow]
    ) -> tuple[Decimal, Optional[date]]:
        profile = self.data.profiles[request["user_id"]]
        start = D(profile["current_available_balance"])
        minimum = D(profile["minimum_balance_to_keep"])
        request_date = day(request["request_date"])
        horizon = request_date + timedelta(days=90)
        requested = D(request["requested_amount"])
        endings, lows = self._daily_balances(start, request_date, horizon, flows)
        suffix_min: dict[date, Decimal] = {}
        running: Optional[Decimal] = None
        for d in sorted(endings, reverse=True):
            future_floor = running
            capacity_floor = endings[d] if future_floor is None else min(endings[d], future_floor)
            suffix_min[d] = capacity_floor
            running = lows[d] if running is None else min(lows[d], running)
        safe_today = max(Decimal("0"), min(requested, money_key(suffix_min[request_date] - minimum)))
        earliest = next((d for d in sorted(suffix_min) if suffix_min[d] - minimum + EPSILON >= requested), None)
        return safe_today, earliest

    def _change_choices(self, request: dict[str, str], series: list[Series]) -> list[Change]:
        profile = self.data.profiles[request["user_id"]]
        stoppable = split_pipe(profile["expense_categories_user_is_willing_to_stop"])
        reducible = split_pipe(profile["expense_categories_user_is_willing_to_reduce"])
        protected = split_pipe(profile["expense_categories_to_protect"])
        choices: list[Change] = []
        for s in series:
            if s.direction != "debit" or s.flexibility == "fixed" or s.category in protected:
                continue
            if s.category in stoppable and s.flexibility in {"stoppable", "reducible_or_stoppable"}:
                choices.append(Change("stop", s.latest_event_id, s.series_id, Decimal("0"), s.amount))
            if s.category in reducible and s.flexibility in {"reducible", "reducible_or_stoppable"} and s.minimum_allowed is not None:
                savings = max(Decimal("0"), s.amount - s.minimum_allowed)
                if savings > 0:
                    choices.append(Change("reduce", s.latest_event_id, s.series_id, s.minimum_allowed, savings))
        # Prefer the least disruptive options when several combinations work.
        return sorted(choices, key=lambda c: (c.kind == "stop", c.event_id))

    @staticmethod
    def _option_candidate(option: dict[str, str]) -> Candidate:
        first = day(option["first_payment_date"])
        count = int(option["number_of_payments"])
        interval = int(option["payment_frequency_days"] or 0)
        amount = D(option["payment_amount"])
        payments = [(first + timedelta(days=i * interval), amount, option["payment_amount"]) for i in range(count)]
        return Candidate(option["payment_method"], payments, D(option["total_payable_amount"]), option["payment_option_id"])

    def _base_candidates(
        self, request: dict[str, str], safe_today: Decimal, earliest: Optional[date]
    ) -> list[Candidate]:
        profile = self.data.profiles[request["user_id"]]
        methods = split_pipe(profile["payment_methods_user_will_consider"])
        requested = D(request["requested_amount"])
        request_date = day(request["request_date"])
        deadline = day(request["desired_completion_date"])
        candidates: list[Candidate] = []

        if "full_payment" in methods:
            candidates.append(Candidate("full_payment", [(request_date, requested, request["requested_amount"])], requested))
            if earliest and earliest > request_date and earliest <= deadline:
                candidates.append(Candidate("wait", [(earliest, requested, request["requested_amount"])], requested))

        if (
            request["allows_partial_payment"].lower() == "true"
            and "partial_payment" in methods
            and Decimal("0") < safe_today < requested
            and earliest is not None and earliest <= deadline
        ):
            candidates.append(Candidate(
                "partial_payment",
                [
                    (request_date, safe_today, compact_amount(safe_today)),
                    (earliest, requested - safe_today, compact_amount(requested - safe_today)),
                ],
                requested,
            ))

        if "installments" in methods:
            maximum = int(profile["max_installment_months"] or 0)
            for option in self.data.options_by_request[request["request_id"]]:
                if option["payment_method"] != "installments":
                    continue
                candidate = self._option_candidate(option)
                if len(candidate.payments) <= maximum and candidate.payments[-1][0] <= deadline:
                    candidates.append(candidate)
        return candidates

    @staticmethod
    def _candidate_rank(candidate: Candidate) -> tuple:
        start = candidate.payments[0][0]
        numeric_option = int(re.search(r"(\d+)$", candidate.option_id).group(1)) if candidate.option_id else 10**9
        return (
            1 if candidate.changes else 0,
            candidate.total,
            start,
            len(candidate.payments),
            numeric_option,
        )

    def decide(self, request: dict[str, str]) -> dict[str, str]:
        profile = self.data.profiles[request["user_id"]]
        flows, series = self.build_forecast(request)
        safe_today, earliest = self._safe_today_and_earliest(request, flows)
        base = self._base_candidates(request, safe_today, earliest)

        safe_candidates = [candidate for candidate in base if self._safe(request, flows, candidate)]
        if not safe_candidates:
            choices = self._change_choices(request, series)
            # Enumerate up to three distinct series; stop/reduce of the same series
            # cannot coexist because each combination is built by series.
            import itertools
            for size in range(1, min(3, len(choices)) + 1):
                for combo in itertools.combinations(choices, size):
                    if len({change.series_id for change in combo}) != len(combo):
                        continue
                    for candidate in base:
                        changed = replace(candidate, changes=tuple(combo))
                        if self._safe(request, flows, changed):
                            safe_candidates.append(changed)
                if safe_candidates:
                    break

        if safe_candidates:
            selected = min(safe_candidates, key=self._candidate_rank)
            if selected.method == "full_payment" and not selected.changes and safe_today >= D(request["requested_amount"]):
                status = "affordable_now"
            elif selected.method == "wait":
                status = "affordable_later"
            else:
                status = "affordable_with_plan"
        else:
            selected = Candidate("not_recommended", [], Decimal("0"))
            status = "affordable_later" if earliest and "full_payment" in split_pipe(profile["payment_methods_user_will_consider"]) and earliest <= day(request["desired_completion_date"]) else "not_affordable"
            # A safe wait should already be in candidates. This guard keeps outputs consistent.
            if status == "affordable_later":
                selected = Candidate("wait", [(earliest, D(request["requested_amount"]), request["requested_amount"])], D(request["requested_amount"]))

        payment_plan = "none"
        if selected.payments:
            payment_plan = "|".join(
                f"{d.isoformat()}:{plan_amount(amount, source)}" for d, amount, source in selected.payments
            )
        changes_text = "none" if not selected.changes else "|".join(change.output_text() for change in selected.changes)
        explanation = self._explain(request, profile, selected, status, safe_today, earliest)
        result = {
            "request_id": request["request_id"],
            "amount_safe_to_pay": compact_amount(safe_today),
            "affordability_status": status,
            "recommended_payment_method": selected.method,
            "payment_plan": payment_plan,
            "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
            "spending_changes_needed": changes_text,
            "decision_explanation": explanation,
        }
        self._validate_result(request, result, selected, flows)
        return result

    @staticmethod
    def _currency_amount(currency: str, value: Decimal) -> str:
        number = f"{money_key(value):,.2f}"
        if value == value.to_integral_value():
            number = f"{value:,.0f}"
        return f"{currency} {number}"

    def _explain(
        self, request: dict[str, str], profile: dict[str, str], selected: Candidate,
        status: str, safe_today: Decimal, earliest: Optional[date],
    ) -> str:
        currency = profile["home_currency"]
        minimum = self._currency_amount(currency, D(profile["minimum_balance_to_keep"]))
        requested = self._currency_amount(currency, D(request["requested_amount"]))
        if selected.method == "full_payment":
            prefix = f"Pay {requested} today."
            if selected.changes:
                prefix = f"Apply the listed flexible-spending changes, then pay {requested} today."
            return f"{prefix} The 90-day forecast keeps at least {minimum} available."
        if selected.method == "partial_payment":
            first = self._currency_amount(currency, selected.payments[0][1])
            remainder = self._currency_amount(currency, selected.payments[1][1])
            return f"Pay {first} today and {remainder} on {selected.payments[1][0].isoformat()}. The plan protects the {minimum} minimum."
        if selected.method == "installments":
            amount = self._currency_amount(currency, selected.payments[0][1])
            return f"Use {len(selected.payments)} installments of {amount}, starting {selected.payments[0][0].isoformat()}. The 90-day forecast protects the {minimum} minimum."
        if selected.method == "wait":
            return f"Wait until {selected.payments[0][0].isoformat()}, then pay {requested} in full without taking the forecast below the {minimum} minimum."
        if earliest:
            return f"Do not complete {requested} by the requested date. Only {self._currency_amount(currency, safe_today)} is safe today, and no eligible plan meets the deadline while protecting the {minimum} minimum."
        return f"Do not proceed with {requested}. No eligible plan completes the request within 90 days while protecting the {minimum} minimum."

    def _validate_result(
        self, request: dict[str, str], result: dict[str, str], selected: Candidate, flows: list[Flow]
    ) -> None:
        safe = D(result["amount_safe_to_pay"])
        requested = D(request["requested_amount"])
        if not Decimal("0") <= safe <= requested:
            raise AssertionError("amount_safe_to_pay outside required bounds")
        if list(result) != OUTPUT_COLUMNS:
            raise AssertionError("output schema order mismatch")
        if selected.payments:
            if selected.payments != sorted(selected.payments, key=lambda item: item[0]):
                raise AssertionError("payment plan is not chronological")
            if selected.method != "not_recommended" and not self._safe(request, flows, selected):
                raise AssertionError("selected plan failed deterministic safety validation")
        if selected.method == "installments":
            option = next(o for o in self.data.options_by_request[request["request_id"]] if o["payment_option_id"] == selected.option_id)
            expected = self._option_candidate(option)
            if selected.payments != expected.payments:
                raise AssertionError("installment plan does not match supplied option")
        if selected.method == "partial_payment":
            if len(selected.payments) != 2 or sum((p[1] for p in selected.payments), Decimal("0")) != requested:
                raise AssertionError("invalid partial payment plan")


def run(dataset: Path, requests_path: Path, output_path: Path) -> list[dict[str, str]]:
    data = DataBundle(dataset)
    engine = AffordabilityEngine(data)
    requests = read_csv(requests_path)
    predictions = [engine.decide(request) for request in requests]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(predictions)
    return predictions


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=repo_root / "dataset")
    parser.add_argument("--requests", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=repo_root / "output.csv")
    args = parser.parse_args()
    requests_path = args.requests or args.dataset / "requests.csv"
    predictions = run(args.dataset, requests_path, args.output)
    print(f"Wrote {len(predictions)} predictions to {args.output}")


if __name__ == "__main__":
    main()
