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
  "version": "0.1.0",
  "schema_version": "1.0",
  "timestamp": "2026-07-04T00:00:00Z",
  "correlation_id": null,
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

## Notes

1. This service provides profit-taking context for other agents.
2. Runtime readiness is reported through `/ready`.
3. Version and schema metadata are reported through `/version`.
4. Existing profit endpoints keep their current response models.
