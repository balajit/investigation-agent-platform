"""Offline, dual-person audited onboarding grant mint/revoke utility.

The command runs under the externally provisioned iap_migrator connection.
Principal names are sourced from the operator's approved identity/audit
procedure; the tool enforces two distinct recorded identities and a digest of
the exact grant/revocation payload. It does not authenticate those operators.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from uuid import UUID, uuid4


def canonical_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _database_url() -> str:
    value = os.environ.get("IAP_ONBOARDING_MIGRATOR_DSN", "").strip()
    if not value:
        raise ValueError("IAP_ONBOARDING_MIGRATOR_DSN is required")
    return value


def _mint(args: argparse.Namespace) -> int:
    from sqlalchemy import create_engine, text

    if args.issuer_id == args.approved_by:
        raise ValueError("issuer and second-person approver must be distinct")
    if args.tenant_id == "__platform__" and not args.issuer_id.strip():
        raise ValueError("platform grants require an authenticated issuer identity")
    start = datetime.fromisoformat(args.valid_from.replace("Z", "+00:00"))
    expiry = datetime.fromisoformat(args.expires_at.replace("Z", "+00:00"))
    if start.tzinfo is None or expiry.tzinfo is None or expiry <= start:
        raise ValueError("validity timestamps must be timezone-aware and expires_at must be later")
    payload: dict[str, object] = {
        "principal_id": args.principal_id,
        "capability": args.capability,
        "tenant_id": args.tenant_id,
        "application_id": args.application_id,
        "environment": args.environment,
        "operation": args.operation,
        "valid_from": start.astimezone(UTC).isoformat(),
        "expires_at": expiry.astimezone(UTC).isoformat(),
        "policy_version": args.policy_version,
        "issuer_id": args.issuer_id,
        "approved_by": args.approved_by,
        "reason": args.reason,
    }
    digest = canonical_digest(payload)
    if args.approval_digest != digest:
        raise ValueError(f"approval digest mismatch; expected {digest}")
    engine = create_engine(_database_url())
    grant_id = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO onboarding_grants "
                    "(id, principal_id, capability, tenant_id, application_id, environment, "
                    "operation, valid_from, expires_at, revoked_at, policy_version, issuer_id, "
                    "approved_by, approval_digest, reason) "
                    "VALUES (:id, :principal_id, :capability, :tenant_id, :application_id, "
                    ":environment, :operation, :valid_from, :expires_at, NULL, :policy_version, "
                    ":issuer_id, :approved_by, :approval_digest, :reason)"
                ),
                {"id": grant_id, **payload, "approval_digest": digest},
            )
            connection.execute(
                text(
                    "INSERT INTO onboarding_grant_audits "
                    "(id, grant_id, action, actor_id, approved_by, approval_digest, reason) "
                    "VALUES (:id, :grant_id, 'MINT', :actor_id, :approved_by, :digest, :reason)"
                ),
                {
                    "id": uuid4(),
                    "grant_id": grant_id,
                    "actor_id": args.issuer_id,
                    "approved_by": args.approved_by,
                    "digest": digest,
                    "reason": args.reason,
                },
            )
    finally:
        engine.dispose()
    print(f"grant_id={grant_id} approval_digest={digest}")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    from sqlalchemy import create_engine, text

    if args.revoker_id == args.approved_by:
        raise ValueError("revoker and second-person approver must be distinct")
    payload: dict[str, object] = {
        "grant_id": str(args.grant_id),
        "revoker_id": args.revoker_id,
        "approved_by": args.approved_by,
        "revocation_reason": args.reason,
    }
    digest = canonical_digest(payload)
    if args.approval_digest != digest:
        raise ValueError(f"approval digest mismatch; expected {digest}")
    engine = create_engine(_database_url())
    try:
        with engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE onboarding_grants SET revoked_at = clock_timestamp(), "
                    "revoked_by = :revoker_id, revocation_approved_by = :approved_by, "
                    "revocation_reason = :revocation_reason "
                    "WHERE id = :grant_id AND revoked_at IS NULL"
                ),
                {
                    "grant_id": args.grant_id,
                    "revoker_id": args.revoker_id,
                    "approved_by": args.approved_by,
                    "revocation_reason": args.reason,
                },
            )
            if result.rowcount != 1:
                raise ValueError("grant not found or already revoked")
            connection.execute(
                text(
                    "INSERT INTO onboarding_grant_audits "
                    "(id, grant_id, action, actor_id, approved_by, approval_digest, reason) "
                    "VALUES (:id, :grant_id, 'REVOKE', :actor_id, :approved_by, :digest, :reason)"
                ),
                {
                    "id": uuid4(),
                    "grant_id": args.grant_id,
                    "actor_id": args.revoker_id,
                    "approved_by": args.approved_by,
                    "digest": digest,
                    "reason": args.reason,
                },
            )
    finally:
        engine.dispose()
    print(f"revoked grant_id={args.grant_id} approval_digest={digest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline dual-person onboarding grant operator.")
    commands = parser.add_subparsers(dest="command", required=True)
    mint = commands.add_parser("mint", help="Mint a grant after an externally verified second-person review.")
    mint.add_argument("--principal-id", required=True)
    mint.add_argument("--capability", required=True)
    mint.add_argument("--tenant-id", required=True, help="Canonical tenant ID or __platform__")
    mint.add_argument("--application-id", required=True)
    mint.add_argument("--environment", required=True)
    mint.add_argument("--operation", required=True)
    mint.add_argument("--valid-from", required=True, help="Timezone-aware ISO-8601 timestamp")
    mint.add_argument("--expires-at", required=True, help="Timezone-aware ISO-8601 timestamp")
    mint.add_argument("--policy-version", required=True)
    mint.add_argument("--issuer-id", required=True)
    mint.add_argument("--approved-by", required=True)
    mint.add_argument("--reason", required=True)
    mint.add_argument("--approval-digest", required=True)
    mint.set_defaults(handler=_mint)

    revoke = commands.add_parser("revoke", help="Revoke a grant after an externally verified second-person review.")
    revoke.add_argument("--grant-id", required=True, type=UUID)
    revoke.add_argument("--revoker-id", required=True)
    revoke.add_argument("--approved-by", required=True)
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--approval-digest", required=True)
    revoke.set_defaults(handler=_revoke)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
