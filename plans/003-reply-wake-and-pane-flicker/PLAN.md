# 003 — Pane refetch loop + team-reply wake (0.8.0)

Two field bugs from vaselin-error-log-tracker (2026-09-05):

## Bug 1 — detail view blinks "Loading…" ~every second

Loop: `GET /api/p/plugin-feedback/tickets/{id}` emits `feedback.updated`
unconditionally (routes.py) → the pane's SSE handler re-runs `openTicket`
whenever the detail view is open (app.js) → `openTicket` blanks the thread
to "Loading…" and GETs again → emits again → forever.

Fix (both ends, either alone would stop the loop):
- routes.py: stop emitting on GET. Reads are not updates; create/reply keep
  their emits.
- app.js: SSE refresh becomes silent — fetch first, swap the DOM after, no
  "Loading…" blank; a busy flag drops re-entrant refreshes. "Loading…" only
  on a fresh open of a different ticket.

## Bug 2 — team replied, no notification

Today the only delivery is a prompt-sections note, throttled to one poll
per 10 min and only surfaced when the owner happens to start a turn. The
reply sat unseen until the pane was opened by hand.

Fix (017 wake-everywhere pattern): `ReplyWakePoller` — a background loop
polling `client.updates()` (5 min cadence; 60s first pass so restarts
deliver promptly; 1h backoff on NotConnected/errors). For each unread
ticket not yet notified, fetch the detail (mark_read=0), route a moment to
the ticket's origin conversation (`context.conversation_id`, stamped at
file time) with ops fallback (`ctx.ops_conversation_id()`); unroutable →
log + mark notified, never a nag. Then emit `feedback.updated` so an open
pane updates live. Moment carries the admin reply text (clamped) plus the
read-and-relay instruction.

## Double-trigger audit (017 standing rule)

| Path | Outcome |
|---|---|
| Dedupe key | `(ticket_id, last_admin_reply_at)` in-memory — one moment per admin reply |
| Prompt-sections unread note | RETIRED when the poller runs (wake replaces delivery); kept as-is on old cores without `send_muted_message` |
| Restart | still-unread replies re-notify once (info still undelivered — convergent, not a nag); read tickets never re-notify (`updates()` filters on `agent_read_at`) |
| mark_read | wake fetch uses `mark_read=0` — unread state stays the owner's, badge stays until they read |
| Pane SSE | poller emit refreshes list/badge; GET no longer emits, so no loop |

## Ship

Version 0.8.0 (manifest + toml + pyproject). Tests: poller dedupe /
routing / old-core note fallback / GET-no-emit. QA on a real Luna, publish
to marketplaces.com.ai `official`, upgrade vaselin-error-log-tracker,
execution summary.
