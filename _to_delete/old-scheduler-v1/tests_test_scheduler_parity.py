"""
The scheduler is standalone, which means one rule now exists twice.

scheduler/money.py holds the scheduler's own copy of "what does this bill
owe" - the rule helpers/domain.py holds for the application. That duplication
is the price of the scheduler importing nothing from the app, and this file is
what stops it becoming a silent divergence: it imports BOTH implementations
and asserts they agree.

If you change how a bill is reconciled, change it in both places. This test
failing is that reminder, and it is the only warning you get.

The same applies to two values the scheduler mirrors: `bill.due_days`, which
the app owns and rent generation reads, and the scheduler.* settings, which the
scheduler owns and the app's settings screen renders.
"""

from decimal import Decimal

import pytest

from helpers import domain as app_domain
from scheduler import money as scheduler_money
from scheduler import settings as scheduler_settings
from services import settings as app_settings


class FakePayment:
    def __init__(self, amount):
        self.amount = Decimal(str(amount))


class FakeBill:
    """The three fields both implementations read, and the ones they write."""

    def __init__(self, amount, penalty, payments):
        self.amount = Decimal(str(amount))
        self.penalty_amount = Decimal(str(penalty))
        self.payments = [FakePayment(p) for p in payments]
        self.paid_amount = Decimal("0")
        self.pending_amount = Decimal("0")
        self.status = "pending"

    def snapshot(self):
        return (str(self.paid_amount), str(self.pending_amount), self.status)


# amount, penalty, payments - chosen to cross every branch of both functions:
# unpaid, part-paid, exactly paid, overpaid, penalised, penalty cleared, zero.
CASES = [
    (10000, 0, []),
    (10000, 500, []),
    (10000, 500, [4000]),
    (10000, 500, [10000]),
    (10000, 500, [10500]),
    (10000, 500, [12000]),
    (10000, 0, [10000]),
    (10000, 0, [3000, 3000, 4000]),
    (0, 0, []),
    (0, 100, []),
    (1234.56, 78.9, [500.5]),
    (99.99, 0.01, [100]),
]


@pytest.mark.parametrize("amount,penalty,payments", CASES)
def test_bill_payable_agrees(amount, penalty, payments):
    app_bill = FakeBill(amount, penalty, payments)
    sched_bill = FakeBill(amount, penalty, payments)
    assert app_domain.bill_payable(app_bill) == scheduler_money.bill_payable(sched_bill)


@pytest.mark.parametrize("amount,penalty,payments", CASES)
def test_reconciliation_agrees(amount, penalty, payments):
    """
    Same inputs, same paid_amount, pending_amount and status.

    This is the one that matters: the penalty task reconciles through the
    scheduler's copy, and every payment route reconciles through the app's. If
    they disagree, a tenant's balance changes depending on which one touched
    the bill last.
    """
    app_bill = FakeBill(amount, penalty, payments)
    sched_bill = FakeBill(amount, penalty, payments)

    app_domain._reconcile_bill(app_bill)
    scheduler_money.reconcile_bill(sched_bill)

    assert app_bill.snapshot() == sched_bill.snapshot()


@pytest.mark.parametrize("value", [Decimal("12.34"), 5, 0, None, 7.5])
def test_decimal_conversion_agrees(value):
    assert app_domain._decimal_to_float(value) == scheduler_money.decimal_to_float(value)


def test_the_scheduler_reads_the_same_bill_due_days_default_the_app_declares():
    """
    rent generation reads `bill.due_days` and falls back to its own copy of the
    default when the admin has never changed it. A mismatch would make an
    auto-generated bill's due date differ from a manually created one.
    """
    app_default = app_settings.DEFAULTS["bill.due_days"]["value"]
    scheduler_default = scheduler_settings.EXTERNAL_DEFAULTS["bill.due_days"]["value"]
    assert app_default == scheduler_default


def test_the_app_serves_exactly_the_scheduler_settings_the_scheduler_declares():
    """One definition, imported - not two lists to keep in step."""
    served = {item["key"] for item in app_settings.describe_for("scheduler")}
    assert served == set(scheduler_settings.SCHEDULER_DEFAULTS)


def test_the_app_never_serves_scheduler_keys_on_its_own_settings_screen():
    main_keys = {item["key"] for item in app_settings.describe_for("main")}
    assert not any(key.startswith("scheduler.") for key in main_keys)
