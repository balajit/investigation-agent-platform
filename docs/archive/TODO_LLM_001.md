# TODO(LLM-001): Generalize LLM Gateway + Fix LLM Call Path

**Status:** Tracked, stub in place — Phase 1 ships with stub, Phase 3.1 completes.

## Goal
Provider-agnostic LLM reasoning (OpenAI ↔ Anthropic switchable via `IAP_LLM_PROVIDER` / `IAP_LLM_MODEL`), structured `InvestigationDecision` output, telemetry metering.

## What Was Done (this commit)
- `ports/reasoning/llm_gateway.py` — `LLMGateway` protocol + `LLMGatewayRequest/Response`
- `infrastructure/reasoning/{factory,openai_adapter,anthropic_adapter}.py` — lazy clients, normalized `LLMCallMetadata`
- `infrastructure/configuration/config.py` — `LLMConfig.provider` (`openai|anthropic`, env `IAP_LLM_PROVIDER`)
- `application/investigation/reasoning.py` — `ReasoningCoordinator` now accepts `llm_gateway` + `observability` opts; calls gateway when present, falls back to stub for vertical slice. Records `record_llm_call`.

## Remaining (Phase 3.1)
- [ ] Add JSON schema validation for `InvestigationDecision` (reject invalid tool actions)
- [ ] Wire `bootstrap/` to create gateway via `create_llm_gateway(config.llm)` and inject into `ReasoningCoordinator`
- [ ] Add integration test `tests/integration/test_llm_gateway.py` with mocked OpenAI/Anthropic
- [ ] Cost/budget enforcement via `guardrails.Budget` + `telemetry.record_llm_call`
- [ ] Prompt-safety + evidence XML isolation end-to-end (already in reasoning.py, needs gateway integration)

## Verification
- `uv run mypy src` — passes
- `uv run pytest` — gateway tests with mock
- Manual: `IAP_LLM_PROVIDER=anthropic IAP_LLM_MODEL=claude-3-5-sonnet...` vs `openai`

## Provider Order
Elastic → Oracle → Code (code deferred to Phase 3.2)
