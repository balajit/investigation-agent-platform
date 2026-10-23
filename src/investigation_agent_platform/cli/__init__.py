# src/investigation_agent_platform/cli/__init__.py
"""Thin operator CLI for tenant onboarding (Phase 4, design D9).

Same service contracts as the REST routes; the CLI is a thin client and
never a second implementation. ``capture`` validates offline; every other
command talks HTTP to the API. No color-dependent output; ``--json`` for
automation.
"""
