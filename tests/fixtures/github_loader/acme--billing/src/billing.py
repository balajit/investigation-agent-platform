"""Shared fixture: mock billing service (Python, no CODEOWNERS).

Covers: same-name symbols across repositories (`charge`, `handle`) for the
cross-repo benchmark case, and the unset-domain registration path.
"""


def charge(account_id: str, amount_cents: int) -> str:
    return f"billed:{account_id}:{amount_cents}"


class BillingService:
    """Same-name `handle` method as PaymentService/PaymentWebhook."""

    def handle(self, event: dict) -> str:
        return f"billing-event:{event.get('type', 'unknown')}"

    def invoice(self, account_id: str) -> str:
        return f"invoiced:{account_id}"
