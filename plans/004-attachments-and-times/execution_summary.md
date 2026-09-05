# 004 — Execution summary

Shipped as 0.9.0 (commit f3a30de), published to marketplaces.com.ai official,
live-verified on vaselin-error-log-tracker 2026-09-05.

## What landed

- **Create sends attachments as fields.** routes.py no longer concatenates
  transcript/agent_context into the body; they ride as payload fields
  (scrubbed, agent_context clamped 200k client-side; service 079 clamps
  again and stores them in the opening message's meta).
- **Pane renders side notes.** Thread messages show the owner's words as the
  body; meta.transcript / meta.agent_context / meta.conversation_excerpt
  render as collapsed `<details>` blocks with char counts. Pane fetches with
  include_attachments=1 (browsers have no token budget).
- **Agent tool keeps its budget.** client.get_ticket and feedback_ticket_get
  gained include_attachments (default false — service elides >4k strings to
  {elided, chars, note} stubs); skill text explains the re-fetch.
- **Times.** List rows use last_activity_at (never bumped by reads); detail
  header shows `Opened X ago · last reply Y ago`; every message shows
  time-ago · absolute datetime.

## Verification

- 78 unit tests pass (new: payload-shape as-fields, agent_context clamp,
  client query params, tool passthrough; updated stale body-concat asserts).
- QA Luna :8766 against the extended fb_stub (mimics service 079): body
  clean, full attachment string through the pane path, last_activity_at in
  list rows.
- Production: hot-upgraded tenant 0.8.0 → 0.9.0 (no restart), filed ticket
  eb5595f3 with a 10k agent context — pane showed one-line body with
  collapsed "Agent context (10,023 chars)", "1m ago · Sep 5, 2026, 4:16 PM",
  header "Opened 1m ago"; list stopped showing "just now" after reads
  ("Agent behaviour: mediocre" correctly reads 2h ago). Ticket closed.
