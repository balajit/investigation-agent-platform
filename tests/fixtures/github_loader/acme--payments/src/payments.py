"""Shared fixture: mock payments service (Python).

Covers: module-level functions, classes with methods, same-name methods
across classes (overload-style), nested functions, and a SQL/ORM table
reference for the benchmark SQL-hop case.
"""


def process_payment(payment_id: str) -> str:
    """Top-level function symbol."""

    def validate() -> bool:
        return bool(payment_id)

    if not validate():
        raise ValueError("invalid payment")
    return f"processed:{payment_id}"


class PaymentService:
    """Primary service class."""

    def charge(self, payment_id: str, amount_cents: int) -> str:
        row = db.session.execute(  # noqa: F821 - fixture intentionally references an unresolved ORM handle
            "SELECT * FROM payments WHERE id = :id", {"id": payment_id}
        ).fetchone()
        if row is None:
            raise LookupError(payment_id)
        return f"charged:{amount_cents}"

    def handle(self, event: dict) -> str:
        return f"payment-event:{event.get('type', 'unknown')}"

    def refund(self, payment_id: str) -> str:
        return f"refunded:{payment_id}"


class PaymentWebhook:
    """Second class with a same-name `handle` method (overload-style case)."""

    def handle(self, event: dict) -> str:
        return f"webhook:{event.get('type', 'unknown')}"
