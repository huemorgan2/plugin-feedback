# Execution summary — plan 003: reply wake + pane flicker (plugin-feedback 0.8.0)

Shipped 2026-09-05. Both bugs Roy reported on luna.com.ai/a/vaselin-error-log-tracker/p/feedback
are fixed and verified live on that tenant.

## Bug 1 — pane flickered between "Loading…" and the thread (~1/sec)

Root cause: `GET /tickets/{id}` emitted `feedback.updated`; the pane's SSE
handler refetches the open ticket on that event; the refetch is another GET,
which emitted again — an infinite loop, each pass blanking the thread to
"Loading…".

Fix (both ends, either alone breaks the loop):
- routes.py: the ticket GET no longer emits `feedback.updated` (reads are not
  updates). Create/reply keep their emits.
- ui/app.js: SSE- and reply-triggered refetches call
  `openTicket(id, {silent: true})` — no "Loading…" blanking, a `ticketBusy`
  guard drops piled-up silent refreshes (never user clicks), and a stale-fetch
  check ignores responses after the user navigated away.

## Bug 2 — team reply arrived with no notification

Root cause: reply awareness was only a throttled prompt-section note (10-min
cache, and only surfaced if the agent happened to get a turn) plus the pane
badge. Nothing proactively told the owner.

Fix: `wake.py` — `ReplyWakePoller`, full 017 wake-everywhere pattern:
- Background task started from `on_server_ready` (core ≥0.92.030); lazy
  `ensure()` from `prompt_sections` covers older cores and hot-swaps.
- First pass 60 s after boot, then every 300 s; 3600 s backoff on
  NotConnected/errors. Never raises out of its loop.
- On an unread admin reply: one muted moment (`channel="moment"`,
  `source="feedback"`, playbooks-028 caps) into the conversation that filed
  the ticket (`ticket.context.conversation_id`), ops-conversation fallback,
  unroutable → logged and dropped (never a nag). Emits `feedback.updated` so
  an open pane refreshes.
- Wake fetch uses `mark_read=False` — unread state stays the owner's; the
  woken agent reads with `feedback_ticket_get`, which marks read.

Double-trigger audit (017 rule: wake REPLACES, never adds):
| Path | Disposition |
| --- | --- |
| Prompt-section unread note | RETIRED when poller runs; kept only on cores without `send_muted_message` |
| Pane badge/SSE | UI-only, kept (not an agent delivery path) |
| Dedupe | `(ticket_id, last_admin_reply_at)`, in-memory, capped 500; newer reply on same ticket notifies again |
| Restart | re-notifies still-unread once (convergent catch-up, not a nag) |

## Tests / QA

- 76 unit tests pass (69 existing + 7 new in tests/test_wake.py, incl.
  route-level "GET never emits").
- Real-Luna QA (port 8766, fb_stub on 8898): exactly one moment in the origin
  conversation at +60 s, agent relayed the reply, no duplicate at +360 s, pane
  route 200s with no emit loop, log clean.

## Ship record

- plugin-feedback 0.8.0 (all three version stamps), commit bcb1aec, pushed to
  huemorgan2/plugin-feedback, published to marketplaces.com.ai official.
- Not baked into the fleet image — marketplace-delivered; no rebake needed.
- Tenant vaselin-error-log-tracker auto-upgraded to 0.8.0 minutes after
  publish (no manual upgrade POST needed). Live verification via CDP + Fly
  logs: /api/plugins reports 0.8.0 active, served app.js contains the
  busy-guard/silent refactor, ticket GETs are single (no 1/sec loop), and the
  poller's `/api/agent/feedback/updates` poll is visible in the machine log.
- Note: that tenant runs image 0.92.040 (newer than the 0.92.030-r1 fleet
  promote from plan 017 item 4) — unrelated to this change.
