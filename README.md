# plugin-feedback

Feedback tickets from any Luna to the Luna team, with threaded replies.

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

## Tools

| tool | policy | gating |
|---|---|---|
| `feedback_ticket_send` | prompt_always | always visible |
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
