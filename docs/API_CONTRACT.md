# Profit_Agent API Contract

This document defines the baseline API contract for `Profit_Agent`.

`Profit_Agent` provides profit-plan, monitoring, and exit-signal advisory output for other agents.

## Standard Headers

```http
Content-Type: application/json
X-Correlation-ID: <uuid>
X-API-KEY: <profit-agent-api-key>
```

## Standard Response Envelope

Operational contract endpoints return this envelope:

```json
{
  "status": "success",
  "agent_type": "profit-agent",
  "version": "0.2.0",
  "schema_version": "profit-decision.v2",
  "timestamp": "2026-07-22T00:00:00Z",
  "correlation_id": "00000000-0000-0000-0000-000000000000",
  "data": {},
  "metadata": {},
  "error": null,
  "confidence_score": null
}
```

## Operational Endpoints

```http
GET /health
GET /ready
GET /version
```

## Profit Endpoints

```http
POST /profit/plan
POST /profit/monitor
POST /profit/exit-signal
```

All profit endpoints accept the same `ProfitPlanRequest`, use the same domain
safety assessment, and return endpoint-specific typed data inside the service
response envelope.

### `POST /profit/plan`

Creates the initial deterministic policy for a newly opened position. Its data
includes:

```json
{
  "initial_stop": 96.0,
  "first_target_price": 108.0,
  "second_target_price": 112.0,
  "trailing_policy": {
    "activation_r": 1.0,
    "trailing_stop_pct": 0.08,
    "reference": "highest_price_since_entry",
    "breach_at_or_below": true
  },
  "partial_exit_policy": {
    "first_target_r": 2.0,
    "second_target_r": 3.0,
    "partial_exit_pct": 0.3
  }
}
```

For backward compatibility during the v2 migration, this response also keeps
the existing decision fields (`primary_action`, `actions`, `decision_id`, and
lifecycle proposal) at the top level. Manager should migrate active-position
evaluation to `/profit/monitor` before these aliases are removed in an
announced future contract.

### `POST /profit/monitor`

Evaluates the latest position state. It returns `current_r`, `profit_stage`,
`recommended_stop`, warnings, the shared advisory decision fields, and target
status that distinguishes threshold reach from Database-confirmed execution.

### `POST /profit/exit-signal`

Returns a compact projection for Risk_Agent:

```json
{
  "should_exit": true,
  "exit_type": "trailing_stop_breach",
  "urgency": "immediate",
  "recommended_quantity": 10,
  "recommended_stop": 110.4,
  "requires_risk_approval": true,
  "advisory_only": true
}
```

Hard-stop and trailing-stop breaches are `immediate`; partial targets use
`normal`; non-exit assessments use `none`. The compact response preserves
deterministic decision identity when lifecycle input is available.

`/health`, `/ready`, and `/version` are open operational endpoints. All
`/profit/*` endpoints require `X-API-KEY`. The service key comes only from
`PROFIT_AGENT_API_KEY`; production startup fails if it is missing. Authentication
failures use the same envelope with HTTP 401 and
`error.code=authentication_failed`.

If `X-Correlation-ID` is present and valid, the response body and header echo
it. Otherwise the service generates a UUID. Validation, authentication,
malformed lifecycle, invalid schema, and internal errors preserve the request
correlation ID without returning request values, secrets, or stack traces.

### Safety precedence

The shared domain decision order is:

1. Validate request invariants.
2. Detect hard stop-loss breach.
3. Calculate R-multiple.
4. Calculate the raw trailing-stop threshold.
5. Detect trailing-stop breach, including equality.
6. Evaluate break-even stop.
7. Evaluate partial exits.
8. Hold when no condition applies.

The legacy `exit_on_stop_breach` input remains accepted for compatibility but
cannot suppress detection of a breached hard stop. Safety detection is
fail-closed; downstream execution still requires `Risk_Agent` approval.

A breached trailing stop returns `primary_action=exit_all`,
`trigger=trailing_stop_breach`, the breached threshold in `recommended_stop`,
and `requires_risk_approval=true`. This is advisory output only.

### Input invariants

The request contract forbids unknown fields and rejects `NaN`, infinity,
non-positive quantities/risks, percentages outside `(0, 1]`, malformed or
whitespace-padded symbols, and invalid target ordering. For a long position:

```text
highest_price_since_entry >= entry_price
highest_price_since_entry >= current_price
stop_loss < entry_price
second_take_profit_r > first_take_profit_r
```

When all risk inputs are supplied, `risk_per_share` must be approximately
`entry_price - stop_loss`. `PROFIT_RISK_MISMATCH_POLICY` supports `reject`,
`warn`, and `recalculate`; it defaults to `reject`.

## Position lifecycle and idempotency

During the v1-to-v2 migration, callers may add Database-owned lifecycle state:

```json
{
  "lifecycle": {
    "position_id": "account-1:position-42",
    "position_version": 7,
    "first_target_executed": false,
    "second_target_executed": false,
    "total_exited_quantity": 0,
    "remaining_quantity": 10
  }
}
```

For an actionable lifecycle-aware recommendation, Profit_Agent returns a
deterministic `decision_id`, `decision_type`, the evaluated
`position_version`, and a proposed `next_lifecycle_state`. For example:

```json
{
  "decision_id": "profit:account-1:position-42:ACGL:v7:tp1",
  "decision_type": "first_take_profit",
  "position_version": 7,
  "next_lifecycle_state": {"first_target_executed": true}
}
```

Target flags are facts supplied by Database_Agent. Profit_Agent never persists
or mutates them. It proposes TP1 first even when both thresholds were crossed,
does not propose an already executed target, and only proposes TP2 after TP1 is
confirmed. Manager must reserve the decision in Database_Agent, pass it through
Risk_Agent, and update target state only after broker-confirmed execution.

`lifecycle.remaining_quantity` must match `position.quantity`, and TP2 cannot be
marked executed while TP1 is false. Lifecycle omission remains temporarily
accepted for the legacy `profit-plan.v1` migration path; such responses do not
contain an idempotency identity and must not be auto-executed.

### Compatibility timeline

- From 2026-07-22, `profit-decision.v2` is the current response contract.
- Requests may omit `schema_version` or explicitly use `profit-plan.v1` during
  migration. Manager accepts these responses only for advisory display, logs a
  deprecation warning, and blocks automatic execution without deterministic
  lifecycle identity.
- Target date 2026-10-31: callers must send/consume `profit-decision.v2`.
  Removal of the legacy path requires a separately announced breaking release;
  it will not occur silently.

## Position Peak Contract

The caller is responsible for sending the highest observed market price for the current position lifecycle:

```json
{
  "position": {
    "symbol": "ADBE",
    "quantity": 20,
    "entry_price": 100.0,
    "current_price": 120.0,
    "stop_loss": 90.0,
    "highest_price_since_entry": 125.0
  }
}
```

`highest_price_since_entry` must be tracked outside this service because `Profit_Agent` is stateless and does not persist positions or market-price history. `Database_Agent` is the source of truth and `Manager_Agent` is expected to forward the stored value.

When the field is omitted or explicitly set to `null`:

1. The endpoint still returns a successful advisory response.
2. The existing fallback remains `max(entry_price, current_price)`.
3. The service adds the following entry to `data.warnings`:

```text
highest_price_since_entry was not provided; trailing stop uses max(entry_price, current_price) as a fallback and may be understated because Profit_Agent does not track price history
```

The warning means the trailing-stop recommendation was produced from incomplete price history and may be lower than the stop that complete history would produce. Downstream callers must not discard this warning.

The warning applies consistently to:

```http
POST /profit/plan
POST /profit/monitor
POST /profit/exit-signal
```

This is a final defensive layer. It does not replace the upstream responsibility to store and forward the real position peak.

## Notes

1. This service provides profit-taking context for other agents.
2. Runtime readiness is reported through `/ready`.
3. Version and schema metadata are reported through `/version`.
4. Profit endpoints use the authenticated `profit-decision.v2` envelope.
5. Missing position-peak history causes a successful response with a warning, not an error.
