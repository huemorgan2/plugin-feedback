# plugin-feedback 001 — reliable error-capture delivery (0.3.0)

## Incident (2026-07-22)
`chat.stream_failed` / `ValueError: Unknown provider: moonshot` was logged at
ERROR on the machine's root logger — the capture handler enqueued it — but it
never reached the control-plane Error Tracking page. Verified live: reproduced
the error, watched the machine log it, and the sink stayed empty. Meanwhile
the same machine's gateway token was invalid (401s on every proxy call).

## Root causes (0.2.x design)
1. **Failed batches were dropped forever.** `flush()` sent one batch and threw
   it away on any failure. `client.report_error` authenticates with the
   machine's gateway token — so the exact incident class the sink exists for
   (dead/rotated token, network outage) silently destroyed its own evidence.
2. **30s flush latency.** ERROR events waited for the next interval tick; a
   machine restart (env push, migrate, crash) inside that window lost the
   queue — and crashes correlate with restarts.

## Fix (0.3.0)
- **Requeue on failure**: a batch that fails to deliver goes back to the head
  of the queue (order preserved) and retries on a later tick. Bounded by the
  existing `deque(maxlen=200)` — no unbounded retry queue. Unconfigured (OSS,
  no control plane) installs drain the queue instead of hoarding it.
- **Fast flush for ERROR+**: an ERROR/CRITICAL record kicks a ~2s debounced
  flush (thread-safe — `logging.Handler.emit` can run on any thread) instead
  of waiting 30s. While deliveries are failing, fast-flush stands down for
  20s (`_FAILURE_BACKOFF_S`) so a dead token doesn't become a POST storm; the
  30s loop keeps retrying.
- `_last_failure` resets on the first successful delivery — backlog then
  drains at one batch (25 events) per flush.

## What this still can't do
If the gateway token stays dead until the machine is destroyed, the queue dies
with the process. That window is covered server-side: the gateway proxy records
a `gateway_auth` event on every invalid-token 401 (luna-service plan 051 /
commit 4278932), so token outages are visible even when the agent can't phone
home. Model-build crashes are additionally captured at the source by luna plan
055 (`model.entry_unusable` ERROR log + degradable chain build).

## Tests
`tests/test_errors.py`: requeue-on-failure (order preserved), recovery
delivers the requeued batch and clears backoff, unconfigured drains without
POSTing, ERROR fast-flush fires in ~10ms, WARNING doesn't fast-flush,
fast-flush stands down during failure backoff. Full suite: 53 passed.
