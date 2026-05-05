# Error Model

Every request through the TIC tree — from a client (e.g. PMS) through `ocabox-server` down to an instrument-end connector (ALPACA, Pilar, IRIS-CCD) — produces a `ValueResponse`. If something goes wrong, `ValueResponse.status == False` and `ValueResponse.error` holds a `ResponseError`. This document specifies the error model: severity semantics, the code registry, what each connector must raise, and how clients should react.

## ResponseError

Defined in `obcom.data_colection.response_error.ResponseError` (shared via the `ocabox-common` package):

```
ResponseError
  code:           int   # see Code registry below
  message:        str
  component_name: str
  severity:       str   # one of TEMPORARY, NORMAL, CRITICAL
  kwargs:         dict  # extra context
```

It travels inside `ValueResponse` via `MessageSerializer` (msgpack). After deserialization it is a full Python `ResponseError` object available at `response.error` on the client side. Clients should use the typed object — do not reach for the dict form.

JSON-ish shape, for debugging:

```json
{
  "address": ["iris", "camera", "ccdtemperature"],
  "status": false,
  "value": null,
  "error": {
    "code": 3002,
    "message": "Method 'ccdtemperature' is not implemented on camera",
    "component_name": "iris_ccd_connector",
    "severity": "CRITICAL"
  }
}
```

## Severity hierarchy

Severity carries client behaviour. As of 2026-05-03 (post `ErrorPolicy.SERVICE` rollout in `ocabox-common` 1.1.0 and PMS), the axis is **retryability**, not error origin.

| Severity     | Meaning                                                  | Default client behaviour                                                                |
|--------------|----------------------------------------------------------|------------------------------------------------------------------------------------------|
| `TEMPORARY`  | Transient — the request might succeed if retried soon.   | `ConditionalCycleQuery` retries silently; the user callback never sees it.               |
| `NORMAL`     | Transient external-state failure with no built-in retry. | Under `ErrorPolicy.SERVICE`: retry with staged backoff (2s × 3 → 10s × 6 → 60s forever). Under `ErrorPolicy.INTERACTIVE`: stop. |
| `CRITICAL`   | Permanent failure. Retrying will not help.               | Always stop. Fire callback once, mark source failed.                                     |

### How to choose severity when raising

Pick the axis answer to: *"if a daemon retried this in 30 seconds, would it succeed?"*

- **Yes, plausibly** → `TEMPORARY` (retry handled inside the cycle-query layer; client never notified) or `NORMAL` (client retries with backoff).
- **No, never under the current configuration** → `CRITICAL`.

Examples:

| Situation                                                            | Severity     | Why                                                                       |
|----------------------------------------------------------------------|--------------|---------------------------------------------------------------------------|
| Single missed poll inside a connector's self-heal window             | `TEMPORARY`  | Connector self-heals; cycle-query retries silently within one cycle.      |
| TCP `ECONNREFUSED` against an instrument (Pilar/IRIS-CCD)            | `NORMAL`     | Sustained device-offline state; SERVICE preset retries with throttled logging until the device returns. |
| ALPACA driver returns `0x40C NotImplemented`                         | `NORMAL`     | External, may change if hardware/driver is reconfigured. (Legacy choice.) |
| Method missing from Pilar/IRIS-CCD command map (`KeyError` path)     | `CRITICAL`   | Address-space mismatch — won't change without a server reconfig.          |
| `no_cachable_regex` excludes the address (`TreeOtherError(4003)`)    | `CRITICAL`   | Configuration, not state — the same address will always be rejected.      |
| Component address doesn't exist in the tree (`AddressError(1002)`)   | `CRITICAL`   | Address space is fixed by component schema.                               |
| Internal request bookkeeping error (e.g. malformed retry counter)    | `CRITICAL`   | TIC bug; abort fast.                                                      |

### TEMPORARY vs NORMAL — blip vs sustained

Both severities describe transient failures, but they differ in **who handles the retry** and **whether the operator sees it**.

- **`TEMPORARY`** — handled inside `ConditionalCycleQuery._send_message` as a silent retry within the cycle. The user callback never fires. Right for *blips* — a single dropped poll, a stale connection that the connector self-heals on the next attempt, a brief timeout while the connector pool reconnects. The operator gets no signal because there is nothing actionable.

- **`NORMAL`** — surfaces to the client. Under `ErrorPolicy.SERVICE` (PMS-style daemons), the client retries with staged backoff (2s × 3 → 10s × 6 → 60s forever) and emits throttled error logs (3 loud + 1/hour). Under `ErrorPolicy.INTERACTIVE`, the client stops and reports. Right for *sustained external-state failures* — device powered off for hours/days/weeks, network partition, instrument in a fault state requiring intervention. The operator should know the source is unreachable for that long.

`ECONNREFUSED` on a TCP connect is unambiguously **sustained**, not a blip — TCP would return a timeout for a single dropped packet, and a refused-connection means no listener at the address. So instrument connectors raise `TreeOtherError(4005, severity=NORMAL)` for `(ConnectionError, BrokenPipeError, OSError, asyncio.TimeoutError, TimeoutError)`. The connector's pool / circuit-breaker logic (when present, e.g. Pilar) absorbs single-blip cases below the raise site, so by the time `_TEMPORARY_IO_ERRORS` fires the device really is offline-for-now and the operator should see it.

Reserve `TEMPORARY` for cases where the connector itself can confidently say "this is a one-off, the next poll will succeed."

### Convention history

The April 2026 convention reserved `CRITICAL` for *errors inside TIC* and `NORMAL` for *errors reflecting external state*. That worked while `NORMAL` and `CRITICAL` behaved identically client-side (`if not TEMPORARY: stop`). When `ErrorPolicy.SERVICE` introduced silent backoff retries for `NORMAL`, the natural axis shifted from *origin* to *retryability*. The current convention reflects that shift; see `Architecture/Error Model across ocabox ecosystem.md` in the ecosystem vault for the full history.

## Code registry

Codes live in `obcom.data_colection.coded_error`. Numeric ranges signal the error class.

### 1xxx — Address errors (`AddressError`)

| Code | Meaning                                          | Default severity | Notes                                                        |
|------|--------------------------------------------------|------------------|--------------------------------------------------------------|
| 1001 | Address does not contain a command               | `NORMAL`         | Malformed address; usually client bug.                       |
| 1002 | Component / method not found in tree             | `CRITICAL`       | Permanent under current tree schema.                         |
| 1003 | Bad request bookkeeping field (e.g. retry count) | `NORMAL`         | Should not occur in practice.                                |
| 1004 | Access denied                                    | `NORMAL`         | Subject to access control state.                             |

### 2xxx — Value errors (`TreeValueError`)

| Code | Meaning                                          | Default severity | Notes                                                        |
|------|--------------------------------------------------|------------------|--------------------------------------------------------------|
| 2001 | Default value error                              | `NORMAL`         | Generic.                                                     |
| 2002 | Value creation failed (downstream raised)        | varies           | Inherits severity from underlying cause where available.     |
| 2003 | Too many retries                                 | varies           | Inherits worst severity from retry attempts.                 |

### 3xxx — Tree-structure errors (`TreeStructureError`)

| Code | Meaning                                          | Default severity | Notes                                                        |
|------|--------------------------------------------------|------------------|--------------------------------------------------------------|
| 3001 | Wrong tree architecture / unexpected leaf        | `CRITICAL`       | TIC misconfiguration.                                        |
| 3002 | Component has not implemented the requested method | `CRITICAL`     | Permanent — instrument doesn't expose this endpoint.         |

### 4xxx — Other / transport errors (`TreeOtherError`)

| Code | Meaning                                          | Default severity | Notes                                                        |
|------|--------------------------------------------------|------------------|--------------------------------------------------------------|
| 4001 | Wrong request                                    | `NORMAL`         |                                                              |
| 4002 | App not answering                                | `NORMAL`         |                                                              |
| 4003 | Request not subscribable (cache deny list)       | `CRITICAL`       | Permanent — `no_cachable_regex` is config, not state.        |
| 4004 | Subscription expired (auto-retried)              | `TEMPORARY`      | Cycle-query handles silently.                                |
| 4005 | Cannot connect to external service               | `NORMAL`         | Connector might come back; transient external state.         |
| 4006 | Incorrectly calculated request timeout           | `CRITICAL`       | TIC bug.                                                     |
| 4007 | Wrong argument                                   | `NORMAL`         |                                                              |

## Per-connector contract

All connectors live under `obsrv/protocols/`. They inherit a base `Connector` with `get`/`put`/`call`. On an **unknown method** they MUST raise `TreeStructureError(code=3002, severity=CRITICAL)`. Returning `None` or a `{"status": "failed"}` dict on a missing-method path is a bug — the client cannot distinguish it from a successful read of a null value and will keep polling forever.

| Connector | File                                          | Behaviour on unknown method                                                  |
|-----------|-----------------------------------------------|------------------------------------------------------------------------------|
| ALPACA    | `obsrv/protocols/alpaca/alpaca_connector.py`  | ALPACA driver returns `0x40C NotImplemented` → wrapped as `TreeValueError(2002, NORMAL)`. Acceptable: external state, retryable in principle. |
| Pilar     | `obsrv/protocols/pilar/pilar_connector.py`    | `KeyError` on command-map lookup → `TreeStructureError(3002, CRITICAL)`.     |
| IRIS-CCD  | `obsrv/protocols/iris_ccd/iris_ccd_connector.py` | Same: `KeyError` → `TreeStructureError(3002, CRITICAL)`. Malformed entries (missing `command` key) also `3002 CRITICAL`. |

For sustained connectivity loss (TCP refused, broken pipe, OS-level socket errors), connectors raise `TreeOtherError(4005, NORMAL)` against the `_TEMPORARY_IO_ERRORS` set so cycle-query subscribers self-recover via the client's `ErrorPolicy.SERVICE` retries when the device returns. For genuine single-poll blips that the connector can absorb internally, `SEVERITY_TEMPORARY` is appropriate — see "TEMPORARY vs NORMAL" above. Connectors must **not** swallow `_TEMPORARY_IO_ERRORS` and return `None`/`{}`: the freezer cannot distinguish that from a successful null read and the operator gets no signal.

## Client behaviour — `ConditionalCycleQuery`

`obcom.comunication.cycle_query.ConditionalCycleQuery._send_message` decides:

1. `status=False` and `code=4004` → silent retry (subscription expired, refresh).
2. `status=False` and `severity=TEMPORARY` → silent retry.
3. `status=False` otherwise → raise `CommunicationRuntimeError`, break the loop.

In case 3, `_execute_callbacks` catches the error and calls the user callback **one last time** with the error-containing response, then stops forever. The user callback sees `status=False` exactly once, then never fires again.

PMS-like consumers should treat any `status=False` callback as terminal for that subscription. The `ErrorPolicy` preset on the consumer side decides whether to start a fresh subscription with backoff or stop permanently.

### Recommended reaction by error

- `code == 3002` — permanent; the instrument doesn't expose this endpoint. Do not resubscribe.
- `code == 4003` — permanent; this request cannot be cached / subscribed. Do not resubscribe.
- `severity == TEMPORARY` — shouldn't reach the callback. If it does, log and ignore.
- Otherwise — subscription has already stopped. Per-service decision whether to retry after cooldown.

## Cross-links

- Ecosystem vault: `Architecture/Error Model across ocabox ecosystem.md` — historical record and cross-project context.
- `ocabox-common` 1.1.0 — `ErrorPolicy` presets that consume severity (`SERVICE`, `INTERACTIVE`).
- This repo, related issues: #6 (Pilar/IRIS-CCD connectors must raise 3002, fixed), #14 (`no_cachable_regex` deny-list narrowing), #15 (severity reclassification).
