# Plan 002 — execution summary

Date: 2026-09-05. Master plan: luna repo `plans/106-validated-bugfix-batch/PLAN.md` (phase 6).

## What shipped

Commit `81cc14b`, version **0.7.0** (pyproject.toml, plugin.json, `plugin_feedback/__init__.py`).

- `plugin_feedback/tools.py`: every ticket send now computes a deterministic
  `client_ref` — `uuid5(NAMESPACE_URL, "luna://feedback/ticket/" + sha256(host|title|body))` —
  sent in the payload so the server (luna-service plan 078) can dedupe with a
  unique index. In-process 10-minute duplicate guard (`_recent_duplicate`,
  `_RECENT_TTL_S = 600`) scoped per plugin load inside `register_tools` (a
  module-global dict leaked across tests/loads); armed only after a successful
  create, so failed sends retry normally.
- `tests/test_idempotency.py`: guard-scoping fixture removed; TTL expiry tested
  by monkeypatching `_RECENT_TTL_S` to -1.

## Verification

- 69/69 tests pass locally.
- Published to the official marketplace: index latest **0.7.0**,
  sha256 `5f8e49e5b303bdf2e112025604096d63de7c5038372bf6ed87c8d6d62cf0b110`.
- Fleet default pinned to 0.7.0 (rollout_image.py `pin`, job-dadsman40ujc73cqvubg)
  and baked into image 0.92.040 (build shows plugin-feedback 0.7.0 with the
  published sha256).

## Notes

- Server-side half (unique index on `feedback_tickets.client_ref`,
  duplicate→200 semantics) shipped in luna-service plan 078, commit `1682f26`,
  migration 0019.
