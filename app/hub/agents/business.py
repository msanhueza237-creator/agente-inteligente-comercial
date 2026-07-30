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
        snapshot = task.payload.get("financial_snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("document_count"):
            return AgentResult(
                summary="Facto aun no entrega un resumen financiero con documentos emitidos.",
                warnings=["Espera la siguiente sincronizacion de Facto y vuelve a solicitar el analisis."],
            )
        revenue = Decimal(str(snapshot.get("net_sales", 0)))
        reference_cost = Decimal(str(snapshot.get("reference_cost_of_sales", 0)))
        reference_margin = revenue - reference_cost
        margin_pct = (
            reference_margin / revenue * 100
            if revenue and snapshot.get("reference_margin_available")
            else Decimal("0")
        )
        warnings = []
        if not snapshot.get("receivables_available"):
            warnings.append(
                "Cuentas por cobrar y vencimientos aun no estan disponibles en la conexion Facto."
            )
        if not snapshot.get("purchases_available"):
            warnings.append(
                "Facto aun no entrega documentos de compra para analizar proveedores."
            )
        if not snapshot.get("cash_balance_available"):
            warnings.append(
                "Bancos y flujo de caja no se incluyen hasta conectar sus fuentes contables."
            )
        purchases = Decimal(str(snapshot.get("net_purchases", 0)))
        purchase_documents = int(snapshot.get("purchase_document_count", 0))
        return AgentResult(
            summary=(
                f"Facto registra ventas netas por CLP {revenue:.0f} en "
                f"{int(snapshot.get('document_count', 0))} documentos. "
                + (
                    f"Las compras netas suman CLP {purchases:.0f} en {purchase_documents} documentos recibidos. "
                    if snapshot.get("purchases_available")
                    else ""
                )
                + (
                    f"El margen bruto referencial es CLP {reference_margin:.0f} ({margin_pct:.1f}%)."
                    if snapshot.get("reference_margin_available")
                    else "El margen queda pendiente hasta completar costos relacionados con las ventas."
                )
            ),
            metrics={
                "net_sales": float(revenue),
                "tax": float(snapshot.get("tax", 0)),
                "gross_sales": float(snapshot.get("gross_sales", 0)),
                "documents": int(snapshot.get("document_count", 0)),
                "net_purchases": float(purchases),
                "purchase_documents": purchase_documents,
                "average_net_ticket": float(snapshot.get("average_net_ticket", 0)),
                "reference_cost": float(reference_cost),
                "reference_margin": float(reference_margin),
                "reference_margin_percent": float(margin_pct),
            },
            evidence=[{"financial_report": snapshot}],
            warnings=warnings,
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
        with_stock = [item for item in products if item.get("stock_known")]
        in_stock = [
            item for item in with_stock if self._number(item, "available_units") > 0
        ]
        out_of_stock = [
            item for item in with_stock if self._number(item, "available_units") <= 0
        ]
        with_source_cost = [
            item for item in products if item.get("cost_available_in_source")
        ]
        total_units = sum(
            (self._number(item, "available_units") for item in with_stock),
            Decimal("0"),
        )
        source_inventory_value = sum(
            (
                self._number(item, "available_units")
                * self._number(item, "unit_cost_source")
                for item in with_source_cost
            ),
            Decimal("0"),
        )

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
                "stock_known": len(with_stock),
                "in_stock": len(in_stock),
                "out_of_stock": len(out_of_stock),
                "total_available_units": float(total_units),
                "source_cost_known": len(with_source_cost),
                "source_inventory_value": float(source_inventory_value),
                "with_sales_history": len(with_sales),
                "slow_moving": len(slow_moving),
                "high_rotation_low_stock": len(high_rotation_low_stock),
            },
            evidence=[
                {
                    "logistics_report": {
                        "top_rotation": [compact(item) for item in ranked_rotation[:10]],
                        "top_margin": [compact(item) for item in ranked_margin[:10]],
                        "top_stock": [
                            compact(item)
                            for item in sorted(
                                in_stock,
                                key=lambda item: self._number(item, "available_units"),
                                reverse=True,
                            )[:20]
                        ],
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
        if task.action == "review_inventory_readiness":
            catalog_count = int(task.payload.get("catalog_count", 0))
            stock_known = int(task.payload.get("stock_known", 0))
            cost_known = int(task.payload.get("cost_known", 0))
            cost_available_in_source = int(task.payload.get("cost_available_in_source", 0))
            cost_requires_usd_conversion = int(task.payload.get("cost_requires_usd_conversion", 0))
            demand_available = int(task.payload.get("demand_available", 0))
            eligible = int(task.payload.get("eligible", 0))
            missing = []
            if demand_available == 0:
                missing.append("ventas por SKU")
            if cost_known == 0 and cost_available_in_source:
                missing.append("conversion verificable del costo a USD")
            elif cost_known == 0:
                missing.append("costo")
            missing_text = " y ".join(missing) or "evidencia suficiente"
            return AgentResult(
                summary=(
                    f"Facto sincronizo {catalog_count} productos: {stock_known} con stock y "
                    f"{cost_available_in_source} con costo de origen. Aun falta {missing_text}; "
                    "por eso no se genero una compra ni una alerta de quiebre. El agente no inventa datos."
                ),
                metrics={
                    "catalog_count": catalog_count,
                    "stock_known": stock_known,
                    "cost_known": cost_known,
                    "cost_available_in_source": cost_available_in_source,
                    "cost_requires_usd_conversion": cost_requires_usd_conversion,
                    "demand_available": demand_available,
                    "eligible": eligible,
                },
                evidence=[
                    {
                        "inventory_readiness": {
                            "source": "facto_read_only",
                            "catalog_count": catalog_count,
                            "stock_known": stock_known,
                            "cost_known": cost_known,
                            "cost_available_in_source": cost_available_in_source,
                            "cost_requires_usd_conversion": cost_requires_usd_conversion,
                            "demand_available": demand_available,
                            "eligible": eligible,
                        }
                    }
                ],
                warnings=[
                    (
                        "Facto no entrego lineas de venta por SKU en los documentos consultados."
                        if demand_available == 0
                        else "Revisa la evidencia de demanda antes de proponer una compra."
                    ),
                    *(
                        ["El costo de Facto esta en moneda de origen y requiere una conversion auditable a USD."]
                        if cost_requires_usd_conversion
                        else []
                    ),
                ],
            )

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
