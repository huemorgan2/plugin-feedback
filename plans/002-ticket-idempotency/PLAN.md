# Plan 002 — ticket idempotency (sibling of luna plans/103, phase 6)

Date: 2026-09-05. Master plan: luna repo `plans/103-validated-bugfix-batch/PLAN.md`.

## Evidence
- 2026-08-31 tickets 011–015: a five-ticket cancel cascade trying to retract ONE
  mis-sent ticket (each "cancel" attempt became a new ticket).
- 2026-09-01 tickets 005–007: a truncation correction + addendum each spawned a
  new ticket (653bd740 thread).
- Validated in this repo (0.6.1): `create_ticket` POSTs a plain body with no
  idempotency key (client.py), `_send` has no dedupe, and there is no
  edit/withdraw API — every correction is a new ticket.

## Date assumption
These paths are unchanged since the events (no commit touched _send/create_ticket
between 08-31 and HEAD), so the gap is current.

## Fix (client side — this repo)
1. Deterministic `client_ref` on every ticket: uuid5 over
   `sha256(host|title|body)`. Sent in the create payload; the service's
   plans/103 sibling adds a unique index + duplicate→200 semantics (luna-service
   repo, its own plan).
2. In-process guard: an identical title+body within 10 minutes returns
   `{sent: false, duplicate_of: <ticket id or ref>}` with a note steering the
   agent to `feedback_ticket_reply` instead of a correction ticket.

Out of scope here: server-side dedupe (luna-service plan), a cancel/withdraw
API (product decision, tracked in the master plan's out-of-scope list).

## Ship
Version 0.6.1 → 0.7.0, marketplace publish, production pin update in
luna-service `plugin-set.toml` (master plan phase 8).
