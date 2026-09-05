# 004 — Attachments as fields + honest times (0.9.0)

Sibling of luna-service plan 079 (ships first). Roy (2026-09-05): a pane
ticket with context attach ON reads as one massive owner message (transcript +
agent context were concatenated into the body), and pane times were built on
`updated_at`, which bumped on mere reads.

## Changes

1. **Pane create sends attachments as fields.** routes.py stops appending
   `transcript` / `agent_context` to the body; they ride as their own payload
   fields (scrubbed and clamped client-side as before; the service clamps
   again and stores them in the opening message's meta). Ticket shape: title /
   user message / agent context / conversation history.

2. **Pane renders attachments as side notes.** The thread shows the owner's
   words as the message body; `meta.transcript`, `meta.agent_context`, and
   `meta.conversation_excerpt` render as collapsed "Attached: …" details
   blocks with sizes. The pane fetches with `include_attachments=1` (browsers
   don't have token budgets).

3. **Agent read keeps its budget.** client.get_ticket gains
   `include_attachments`; the feedback_ticket_get tool exposes it (default
   false — the service elides >4k attachment strings to `{chars, elided,
   note}`); skill text explains how to pull the full context.

4. **Times.**
   - List rows: `last_activity_at` (service-computed: created/last replies
     max — never bumped by reads) instead of `updated_at`.
   - Detail header: `Opened 3d ago · last reply 1d ago` (reply part only when
     the thread has replies).
   - Every message: time-ago **and** absolute datetime.

## Compat

New plugin + old service: attachment fields ignored server-side (context would
be lost) — luna-service 079 deploys first. Old plugin + new service: unchanged.

## QA

Unit tests (routes payload shape, tool param passthrough) + real-Luna pane
check with the fb stub extended to echo the new fields; live verify on
vaselin-error-log-tracker after publish.
