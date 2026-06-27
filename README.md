# Profit Agent

Profit Agent creates advisory profit-management plans for open positions in the multi-agent trading system.

It does **not** place sell orders. It returns suggested actions for `Manager_Agent`, `Risk_Agent`, and `Execution_Agent` to review and execute through the normal guarded flow.

## Responsibilities

- Detect stop-loss breach
- Calculate R-multiple
- Recommend break-even stop movement
- Recommend trailing stop movement
- Recommend partial take profit
- Return advisory exit signal metadata

## API

### Health

```bash
curl http://localhost:8011/health
```

### Profit Plan

```bash
curl -X POST http://localhost:8011/profit/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "position": {
      "symbol": "ADBE",
      "quantity": 20,
      "entry_price": 100,
      "current_price": 120,
      "stop_loss": 90
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
  ]
}
```

## Endpoints

```text
GET  /health
POST /profit/plan
POST /profit/monitor
POST /profit/exit-signal
```

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8011
```

## Tests

```bash
ruff check app tests
pytest -q
```

## Docker

```bash
docker build -t profit-agent .
docker run --rm -p 8011:8011 profit-agent
```

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
