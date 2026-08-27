"""
scheduler/money.py - what a bill owes, computed here rather than imported.

These three functions are the scheduler's copy of a rule the application also
holds (helpers/domain.py). That is a deliberate, and the only, duplication in
this package: the alternative was importing the app, which is the coupling
this folder exists to avoid.

The duplication is guarded, not hoped about. tests/test_scheduler_parity.py in
the APPLICATION's test suite imports both implementations and asserts they
agree across a matrix of amounts, payments and penalties. That test fails the
moment either side changes without the other - which is the whole point of
having it, and is why changing the penalty or reconciliation rule means
changing it in both places and running the app's suite.
"""

from decimal import Decimal


def decimal_to_float(value) -> float:
    """Money out of the database is Decimal; arithmetic here is float."""
    return float(value) if isinstance(value, Decimal) else (value or 0.0)


def bill_payable(bill) -> float:
    """
    What is actually owed on a bill: the original amount plus any late penalty.

    `bill.amount` is deliberately never touched by the penalty task, so it
    always answers "what was this bill for". This answers the different
    question "what must be paid", and is the only place the two are added.
    """
    return decimal_to_float(bill.amount) + decimal_to_float(bill.penalty_amount)


def reconcile_bill(bill) -> None:
    """
    Recompute paid_amount, pending_amount and status from linked payments.

    The single place in this process that decides what a bill owes - which is
    why adding the penalty here is enough for the figure to be correct
    everywhere the bill is later read.
    """
    total_paid = sum(decimal_to_float(p.amount) for p in bill.payments)
    payable = bill_payable(bill)

    bill.paid_amount = Decimal(str(total_paid))
    bill.pending_amount = Decimal(str(max(0.0, payable - total_paid)))

    if total_paid <= 0:
        bill.status = "pending"
    elif total_paid >= payable:
        bill.status = "paid"
    else:
        bill.status = "partial"
