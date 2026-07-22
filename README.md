# Profit Agent

Profit Agent creates advisory profit-management plans for open positions in the multi-agent trading system.

It does **not** place sell orders. It returns suggested actions for `Manager_Agent`, `Risk_Agent`, and `Execution_Agent` to review and execute through the normal guarded flow.

## Responsibilities

- Detect stop-loss breach
- Calculate R-multiple
- Recommend break-even stop movement
- Recommend trailing stop movement
- Recommend partial take profit
- Suppress already executed take-profit stages using Database-owned lifecycle state
- Return advisory exit signal metadata
- Apply a deterministic regime-aware policy only after hard safety checks

Hard stop-loss and active trailing-stop breaches always take priority over
break-even and take-profit advice. A trailing threshold is not discarded when
the latest price crosses it; the service returns an advisory `exit_all` with
`trigger=trailing_stop_breach`, `requires_risk_approval=true`, and never sends
an order itself.

## Position peak responsibility

`Profit_Agent` is stateless. It does not store positions, market-price history, or the highest price observed after entry.

The caller must track and send `position.highest_price_since_entry` for the current position lifecycle. The expected integration flow is:

```text
Database_Agent stores the position peak
  -> Manager_Agent reads and forwards it
  -> Profit_Agent calculates the trailing-stop advisory
```

When the caller omits the field or sends `null`, the request is still accepted. For backward compatibility, the service continues to use:

```text
max(entry_price, current_price)
```

as a fallback. The response then includes this warning instead of silently presenting the fallback as complete history:

```text
highest_price_since_entry was not provided; trailing stop uses max(entry_price, current_price) as a fallback and may be understated because Profit_Agent does not track price history
```

A successful response with this warning is degraded advisory output, not an error. Downstream agents should preserve and review the warning before acting on the recommended stop.

## API

### Health

```bash
curl http://localhost:8011/health
```

### Profit Plan

```bash
curl -X POST http://localhost:8011/profit/plan \
  -H 'Content-Type: application/json' \
  -H 'X-API-KEY: replace-with-service-secret' \
  -H 'X-Correlation-ID: 00000000-0000-0000-0000-000000000000' \
  -d '{
    "position": {
      "symbol": "ADBE",
      "quantity": 20,
      "entry_price": 100,
      "current_price": 120,
      "stop_loss": 90,
      "highest_price_since_entry": 125
    },
    "first_take_profit_r": 2.0,
    "partial_exit_pct": 0.30
  }'
```

Example response fields:

```json
{
  "symbol": "ADBE",
  "current_r_multiple": 2.0,
  "primary_action": "partial_exit",
  "actions": [
    {
      "action": "partial_exit",
      "quantity": 6,
      "reason": "Position reached first take-profit target at 2.0R"
    }
  ],
  "warnings": []
}
```

For idempotent orchestration, include the current lifecycle from
`Database_Agent`:

```json
{
  "lifecycle": {
    "position_id": "account-1:position-42",
    "position_version": 7,
    "first_target_executed": false,
    "second_target_executed": false,
    "total_exited_quantity": 0,
    "remaining_quantity": 20
  }
}
```

Profit_Agent then returns a deterministic `decision_id`. It does not update the
lifecycle itself; `Manager_Agent` must reserve the decision and only ask
`Database_Agent` to mark a target executed after a confirmed fill.

## Endpoints

```text
GET  /health
GET  /ready
GET  /version
GET  /metrics
POST /profit/plan
POST /profit/monitor
POST /profit/exit-signal
```

All three profit endpoints accept the same validated position/lifecycle input,
share one safety decision engine, and return the same missing-peak warning
behavior. Their response data is intentionally different:

- `/profit/plan` returns the initial stop, first/second target prices, trailing
  policy, and partial-exit policy.
- `/profit/monitor` returns current R, profit stage, recommended stop, target
  reached/executed state, and warnings.
- `/profit/exit-signal` returns a compact Risk-gate projection with
  `should_exit`, `exit_type`, urgency, and recommended quantity.

During the v2 migration, `/profit/plan` retains the legacy decision fields at
the top level so the current Manager can migrate without a silent contract
break. New monitoring callers should use `/profit/monitor`; Risk-gate callers
should use `/profit/exit-signal`.

All six endpoints use response contract `profit-decision.v2` and service
version `0.2.0`. `/health`, `/ready`, and `/version` are operational endpoints
and remain unauthenticated without exposing secrets. Every `/profit/*` endpoint
requires the exact shared service key in `X-API-KEY`. Key comparison is
constant-time, and the key is never included in responses.

The service accepts `X-Correlation-ID` and returns the same value in the body
and response header. If omitted or malformed, it creates a UUID. Manager must
forward this ID to Profit, Risk, Database decision records, and Execution.

```env
APP_ENV=production
PROFIT_AGENT_API_KEY=
```

Production startup fails when `PROFIT_AGENT_API_KEY` is empty. The checked-in
`.env.example` intentionally contains no secret value.

Requests reject unknown fields, non-finite numbers, malformed symbols, invalid
long-position price relationships, and take-profit targets that are not
strictly ordered. If `entry_price`, `stop_loss`, and `risk_per_share` are all
provided, the risk value must match `entry_price - stop_loss` within floating
point tolerance.

## Runtime policy

```env
PROFIT_RISK_MISMATCH_POLICY=reject
```

Supported values are `reject`, `warn`, and `recalculate`. The production-safe
default is `reject`. Both non-reject policies add a response warning;
`recalculate` also replaces the supplied value with the derived risk. Manager
must preserve warnings and still send every actionable advisory through
`Risk_Agent` before `Execution_Agent`.

## Adaptive profit policy

Manager may send `market_context` from the versioned Market Regime and
Technical projections. The deterministic `deterministic_adaptive_v1` policy
can widen a trail in a bull/strong-trend regime, use ATR evidence in a volatile
regime, or tighten targets and stops in a bear/weak-trend regime. It returns the
base and adjusted values plus `adjustment_reasons`; no LLM chooses execution
numbers.

Emergency halt, hard stop-loss, the base trailing stop, stale market data,
incomplete peak history, and stale position version all take precedence. An
explicitly failed `data_quality` check returns `primary_action=review` and
`decision_status=blocked`, with no executable decision identity. An adaptive
policy can create a tighter trailing breach, but can never hide a breach of the
base policy.

```env
PROFIT_MARKET_DATA_MAX_AGE_SECONDS=120
```

When adaptive context is present, callers must either provide a timezone-aware
`market_context.observed_at` within this age or explicitly attest freshness in
`data_quality.market_price_fresh`. Legacy requests without adaptive context
retain the static policy during the compatibility window.

## Decimal and market constraints

All price, R-multiple, target, trailing-stop, and quantity arithmetic uses
`Decimal` in the domain layer. Callers may provide exact constraints:

```json
{
  "market_constraints": {
    "price_increment": "0.01",
    "quantity_increment": "0.000001",
    "minimum_order_quantity": "0.000001"
  }
}
```

Prices and quantities are floored to an actual multiple of their increment.
Partial exits never exceed the Database-owned remaining quantity. If rounding
would produce less than the minimum order, Profit returns a blocked review and
does not emit a zero-quantity partial exit. Constraint values serialize as
exact decimal strings; existing monetary and quantity output fields remain JSON
numbers for v2 compatibility.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8011
```

## Tests

```bash
ruff check app tests scripts
ruff format --check app tests scripts
mypy app
coverage run -m pytest -q
coverage report --fail-under=90
pip-audit -r requirements.txt
bandit -r app -ll
python scripts/validate_openapi.py
```

## Docker

```bash
docker build -t profit-agent .
docker run --rm -p 8011:8011 \
  -e APP_ENV=production \
  -e PROFIT_AGENT_API_KEY="$PROFIT_AGENT_API_KEY" \
  profit-agent
```

The production image pins Python 3.12.13 on slim-bookworm, installs only
`requirements.txt`, runs as UID/GID 10001, disables Uvicorn access/server
headers, and handles SIGTERM with a 30-second graceful shutdown window.

Runtime configuration:

```env
PROFIT_AGENT_HOST=0.0.0.0
PROFIT_AGENT_PORT=8011
PROFIT_AGENT_WORKERS=1
PROFIT_AGENT_LOG_LEVEL=INFO
PROFIT_MAX_REQUEST_BODY_BYTES=65536
PROFIT_REQUEST_TIMEOUT_SECONDS=30
PROFIT_RATE_LIMIT_PER_MINUTE=120
```

`GET /metrics` exposes Prometheus counters/histograms for requests, validation,
hard/trailing breaches, partial and duplicate decisions, peak fallback, and
latency. Metrics use endpoint/status labels only; symbols, account IDs, and
secrets are never metric labels. Application logs are one-line JSON and include
correlation/decision/position identifiers and action reasons, but never API
keys, authorization headers, broker credentials, database URLs, or raw request
bodies. The in-memory rate limit is per worker, so deployments with multiple
workers must size `PROFIT_RATE_LIMIT_PER_MINUTE` accordingly or add a shared
gateway limiter.

## Integration rule

`Profit_Agent` is advisory only. It should never call `Execution_Agent` directly.

Recommended flow:

```text
Database_Agent
  -> Manager_Agent
  -> Profit_Agent
  -> Risk_Agent
  -> Execution_Agent
```