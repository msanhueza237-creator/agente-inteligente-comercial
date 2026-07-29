from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

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


class LogisticsAgent(BusinessAgent):
    """Turns Facto inventory snapshots into reviewable cross-agent signals."""

    @staticmethod
    def _number(row: dict[str, Any], key: str) -> Decimal:
        return Decimal(str(row.get(key) or 0))

    async def execute(self, task: HubTask) -> AgentResult:
        products = [item for item in task.payload.get("products", []) if isinstance(item, dict)]
        if not products:
            return AgentResult(
                summary="No hay snapshots logisticos con evidencia de Facto para analizar.",
                warnings=["Sin productos sincronizados o sin campos de inventario disponibles"],
            )

        with_sales = [item for item in products if item.get("sales_history_available")]
        ranked_rotation = sorted(
            products,
            key=lambda item: self._number(item, "average_daily_demand"),
            reverse=True,
        )
        ranked_margin = sorted(
            [item for item in products if item.get("price_known") and item.get("cost_known")],
            key=lambda item: self._number(item, "margin_percent"),
            reverse=True,
        )
        slow_moving = [
            item for item in with_sales
            if item.get("stock_known")
            and self._number(item, "available_units") > 0
            and self._number(item, "units_sold_observed") == 0
        ]
        high_rotation_low_stock = [
            item for item in ranked_rotation
            if item.get("stock_known")
            and self._number(item, "average_daily_demand") > 0
            and self._number(item, "available_units")
            <= self._number(item, "average_daily_demand") * Decimal("30")
        ]

        def compact(item: dict[str, Any]) -> dict[str, Any]:
            return {
                "sku": item.get("sku"),
                "name": item.get("name"),
                "stock": item.get("available_units"),
                "daily_demand": item.get("average_daily_demand"),
                "margin_percent": item.get("margin_percent"),
            }

        proposals: list[ActionProposal] = []
        if slow_moving:
            proposals.append(
                ActionProposal(
                    kind=ProposalKind.campaign_draft,
                    title="Borrador comercial para inventario de baja rotacion",
                    summary=(
                        f"Se detectaron {len(slow_moving)} SKU con stock y sin ventas en el periodo observado. "
                        "Marketing y Comercial deben revisar antes de crear una campana."
                    ),
                    payload={
                        "source_agent": "logistics",
                        "next_agents": ["marketing", "commercial"],
                        "sku_candidates": [compact(item) for item in slow_moving[:20]],
                    },
                    risk_level="medium",
                )
            )
        if high_rotation_low_stock:
            proposals.append(
                ActionProposal(
                    kind=ProposalKind.executive_alert,
                    title="Revision de importacion para SKU de alta rotacion",
                    summary=(
                        f"{len(high_rotation_low_stock)} SKU de alta rotacion tienen cobertura menor a 30 dias. "
                        "Comercio exterior debe calcular la compra con costo y demanda antes de aprobarla."
                    ),
                    payload={
                        "source_agent": "logistics",
                        "next_agents": ["foreign_trade", "finance", "executive"],
                        "sku_candidates": [compact(item) for item in high_rotation_low_stock[:20]],
                    },
                    risk_level="high",
                )
            )

        return AgentResult(
            summary=(
                f"Analisis logistico preparado: {len(products)} SKU, {len(with_sales)} con historial de ventas, "
                f"{len(slow_moving)} de baja rotacion y {len(high_rotation_low_stock)} con cobertura corta."
            ),
            metrics={
                "products": len(products),
                "with_sales_history": len(with_sales),
                "slow_moving": len(slow_moving),
                "high_rotation_low_stock": len(high_rotation_low_stock),
            },
            evidence=[
                {
                    "logistics_report": {
                        "top_rotation": [compact(item) for item in ranked_rotation[:10]],
                        "top_margin": [compact(item) for item in ranked_margin[:10]],
                        "slow_moving": [compact(item) for item in slow_moving[:20]],
                        "high_rotation_low_stock": [compact(item) for item in high_rotation_low_stock[:20]],
                    }
                }
            ],
            proposals=proposals,
            warnings=(
                []
                if with_sales
                else ["Facto aun no entrego un periodo de ventas suficiente para detectar baja rotacion"]
            ),
        )


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
            evidence=[
                {
                    "inventory_recommendation": {
                        "sku": result.sku,
                        "available_units": position.available_units,
                        "committed_units": position.committed_units,
                        "confirmed_inbound_units": position.confirmed_inbound_units,
                        "reorder_point_units": result.reorder_point_units,
                        "target_units": result.target_units,
                        "recommended_units": result.recommended_units,
                        "recommended_value_usd": str(result.recommended_value_usd),
                        "required_order_date": (
                            result.required_order_date.isoformat() if result.required_order_date else None
                        ),
                        "projected_stockout_date": (
                            result.projected_stockout_date.isoformat() if result.projected_stockout_date else None
                        ),
                        "severity": result.severity,
                        "purchase_policy": result.purchase_policy,
                        "warnings": list(result.warnings),
                    }
                }
            ],
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
