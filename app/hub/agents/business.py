from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.foreign_trade.planning import ForeignTradePlanner, InventoryPosition
from app.hub.agents.base import BusinessAgent
from app.hub.contracts import ActionProposal, AgentResult, HubTask, ProposalKind


class CommercialAgent(BusinessAgent):
    async def execute(self, task: HubTask) -> AgentResult:
        overdue = int(task.payload.get("overdue_follow_ups", 0))
        proposal = ActionProposal(
            kind=ProposalKind.commercial_follow_up,
            title="Seguimientos comerciales para revisar",
            summary=f"Se detectaron {overdue} seguimientos vencidos.",
            payload={"company_ids": task.payload.get("company_ids", [])},
        )
        return AgentResult(summary=proposal.summary, metrics={"overdue": overdue}, proposals=[proposal])


class MarketingAgent(BusinessAgent):
    async def execute(self, task: HubTask) -> AgentResult:
        segment = str(task.payload.get("segment", "sin segmento"))
        proposal = ActionProposal(
            kind=ProposalKind.campaign_draft,
            title=f"Borrador de campana para {segment}",
            summary="Se preparo un borrador; no se enviara hasta ser aprobado en el CRM.",
            payload={"segment": segment, "channel": task.payload.get("channel", "email")},
        )
        return AgentResult(summary=proposal.summary, proposals=[proposal])


class FinanceAgent(BusinessAgent):
    async def execute(self, task: HubTask) -> AgentResult:
        revenue = Decimal(str(task.payload.get("revenue", 0)))
        cost = Decimal(str(task.payload.get("cost", 0)))
        margin = revenue - cost
        margin_pct = (margin / revenue * 100) if revenue else Decimal("0")
        return AgentResult(
            summary=f"Margen estimado: USD {margin:.2f} ({margin_pct:.1f}%).",
            metrics={"revenue": float(revenue), "cost": float(cost), "margin": float(margin)},
        )


class CollectionsAgent(BusinessAgent):
    async def execute(self, task: HubTask) -> AgentResult:
        overdue = Decimal(str(task.payload.get("overdue_amount", 0)))
        proposal = ActionProposal(
            kind=ProposalKind.collection_reminder,
            title="Recordatorios de cobranza para aprobar",
            summary=f"Cartera vencida informada: {overdue:.2f}. No se enviaran mensajes automaticamente.",
            payload={"invoice_ids": task.payload.get("invoice_ids", [])},
            risk_level="high",
        )
        return AgentResult(summary=proposal.summary, metrics={"overdue_amount": float(overdue)}, proposals=[proposal])


class ForeignTradeAgent(BusinessAgent):
    def __init__(self) -> None:
        self.planner = ForeignTradePlanner()

    async def execute(self, task: HubTask) -> AgentResult:
        position = InventoryPosition(
            sku=str(task.payload["sku"]),
            available_units=int(task.payload.get("available_units", 0)),
            committed_units=int(task.payload.get("committed_units", 0)),
            confirmed_inbound_units=int(task.payload.get("confirmed_inbound_units", 0)),
            average_daily_demand=Decimal(str(task.payload.get("average_daily_demand", 0))),
            unit_cost_usd=Decimal(str(task.payload.get("unit_cost_usd", 0))),
        )
        result = self.planner.recommend(
            position,
            as_of=date.fromisoformat(str(task.payload.get("as_of", date.today().isoformat()))),
            demand_multiplier=Decimal(str(task.payload.get("demand_multiplier", 1))),
        )
        proposal = ActionProposal(
            kind=ProposalKind.purchase_order,
            title=f"Reposicion sugerida {result.sku}",
            summary=(
                f"Sugerencia: {result.recommended_units} unidades por "
                f"USD {result.recommended_value_usd}. Politica: {result.purchase_policy}."
            ),
            payload={
                "sku": result.sku,
                "units": result.recommended_units,
                "value_usd": str(result.recommended_value_usd),
                "required_order_date": (
                    result.required_order_date.isoformat() if result.required_order_date else None
                ),
                "policy": result.purchase_policy,
            },
            risk_level="high",
        )
        return AgentResult(
            summary=proposal.summary,
            metrics={
                "net_units": result.net_units,
                "reorder_point_units": result.reorder_point_units,
                "target_units": result.target_units,
            },
            proposals=[proposal] if result.recommended_units else [],
            warnings=list(result.warnings),
        )


class ExecutiveAgent(BusinessAgent):
    async def execute(self, task: HubTask) -> AgentResult:
        alerts = list(task.payload.get("alerts", []))
        return AgentResult(
            summary=f"Resumen ejecutivo preparado con {len(alerts)} alertas prioritarias.",
            metrics={"alerts": len(alerts)},
            evidence=[{"alert": alert} for alert in alerts[:20]],
        )
