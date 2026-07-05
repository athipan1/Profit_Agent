from __future__ import annotations

from fastapi import FastAPI

from app.models import (
    HealthData,
    ProfitPlanData,
    ProfitPlanRequest,
    StandardAgentResponse,
)
from app.service import build_profit_plan
from app.system_contract import router as system_contract_router


app = FastAPI(
    title="Profit Agent",
    description="Profit-taking advisory service for the multi-agent trading system.",
    version="0.1.0",
)
app.include_router(system_contract_router)


@app.get("/health", response_model=StandardAgentResponse[HealthData])
def health() -> StandardAgentResponse[HealthData]:
    return StandardAgentResponse(status="success", data=HealthData())


@app.post("/profit/plan", response_model=StandardAgentResponse[ProfitPlanData])
def profit_plan(request: ProfitPlanRequest) -> StandardAgentResponse[ProfitPlanData]:
    data = build_profit_plan(request)
    return StandardAgentResponse(status="success", data=data)


@app.post("/profit/monitor", response_model=StandardAgentResponse[ProfitPlanData])
def profit_monitor(request: ProfitPlanRequest) -> StandardAgentResponse[ProfitPlanData]:
    data = build_profit_plan(request)
    return StandardAgentResponse(status="success", data=data)


@app.post("/profit/exit-signal", response_model=StandardAgentResponse[ProfitPlanData])
def profit_exit_signal(request: ProfitPlanRequest) -> StandardAgentResponse[ProfitPlanData]:
    data = build_profit_plan(request)
    return StandardAgentResponse(status="success", data=data)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"message": "Profit Agent is running"}
