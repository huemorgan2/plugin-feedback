# plugin-feedback

Feedback tickets from any Luna to the Luna team, with threaded replies —
plus the runtime's silent error-capture side channel (since 0.2.0).

## What it does

- **Agent-filed feedback.** When the owner complains directly ("this is
  expensive", "this doesn't work"), the agent offers to send feedback and
  drafts it with `feedback_ticket_send`. When the frustration is indirect (repeated
  failures, the owner angry at the agent), the agent files it itself with
  `written_by="agent"`, attaches the conversation as reference, and tells the
  owner it did. `feedback_ticket_send` is `prompt_always`: the approval card shows
  the owner exactly what leaves the machine.
- **Owner-written feedback.** A **Feedback** pane in the sidebar: write a
  ticket, see the team's answers, reply on the thread.
- **Reply awareness.** A throttled check (at most every 10 minutes, inside
  `prompt_sections`) injects a one-line note when the team replied; the agent
  loads the `feedback-tickets` skill, reads the thread with `feedback_ticket_get`,
  and relays it in plain words.

## Error capture (0.2.0, plan 007)

A silent side channel — **no tickets**. Three feeds land in the control
plane's `error_events` table (luna-service plan 051) and show up in the
admin Error Tracking view:

1. **Agent-side runtime errors.** A `logging.Handler` on the root logger at
   WARNING+ (so `uvicorn.error` / unhandled ASGI tracebacks are caught).
   `emit` only enqueues to a bounded drop-oldest queue; a background task
   batches to `POST /api/agent/errors` with the gateway token. Records from
   the plugin's own loggers and the HTTP stack are skipped (recursion guard).
2. **Browser UI errors.** `ui/reporter.js` — served at
   `/api/p/plugin-feedback/reporter.js` and injected into proxied pages by
   luna-service — captures `window.onerror`, unhandled rejections,
   resource-load failures, failed/5xx/slow fetches, with a 20-entry
   breadcrumb ring, client-side dedupe, and a per-minute cap. It posts to
   the plugin's `/errors` route with the Shell's bearer token; the plugin
   forwards with the gateway token, so the browser never sees it.
3. **Explicit agent reports.** The `report_issue` tool records
   agent-noticed problems silently.

Everything is best-effort and degrades to a no-op on OSS installs without a
control plane.

## Tools

| tool | policy | gating |
|---|---|---|
| `feedback_ticket_send` | prompt_always | always visible |
| `report_issue` | auto_approve | always visible |
| `feedback_ticket_list` | auto_approve | `feedback-tickets` skill |
| `feedback_ticket_get` | auto_approve | `feedback-tickets` skill |
| `feedback_ticket_reply` | prompt_always | `feedback-tickets` skill |

## Backend

All ticket state lives on the luna-service control plane (its plan
`046-feedback-tickets` defines the contract: `/api/agent/feedback/*`). The
plugin is stateless and authenticates with the machine's gateway token
(`LUNA_GATEWAY_URL` + `LUNA_GATEWAY_TOKEN`; override with
`LUNA_FEEDBACK_SERVICE_URL` + `LUNA_FEEDBACK_TOKEN` for self-hosted/test
setups). Without credentials the tools degrade to a readable
"not connected" error.

Every outbound field is scrubbed for credential-shaped strings, and ticket
context (agent name, owner, mission, conversation id, exact UTC client time,
host) is stamped automatically — the server adds machine identity from the
resolved agent row.

## Tests

```bash
python -m pytest tests/ -q
```
