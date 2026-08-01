from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.foreign_trade.catalog import build_foreign_trade_report
from app.foreign_trade.planning import ForeignTradePlanner, InventoryPosition
from app.hub.agents.base import BusinessAgent
from app.hub.commercial import build_commercial_report
from app.hub.contracts import ActionProposal, AgentResult, HubTask, ProposalKind
from app.hub.marketing import build_marketing_report


class CommercialAgent(BusinessAgent):
    async def execute(self, task: HubTask) -> AgentResult:
        snapshot = [
            row for row in task.payload.get("commercial_snapshot", []) if isinstance(row, dict)
        ]
        companies = [
            row for row in task.payload.get("crm_companies", []) if isinstance(row, dict)
        ]
        if not snapshot and not companies:
            return AgentResult(
                summary="Aun no existe una cartera sincronizada para analizar.",
                warnings=[
                    "Espera la siguiente sincronizacion de Facto y Tiendanube, luego vuelve a solicitar el analisis."
                ],
            )

        financial_snapshot = task.payload.get("financial_snapshot")
        report = build_commercial_report(
            snapshot,
            companies,
            financial_snapshot if isinstance(financial_snapshot, dict) else None,
        )
        metrics = report["metrics"]
        proposals = [
            ActionProposal(
                kind=ProposalKind.campaign_draft,
                title=f"Segmento para revisar: {segment['name']}",
                summary=(
                    f"{segment['count']} clientes cumplen reglas trazables. "
                    "La campana se mantiene como borrador y no enviara mensajes automaticamente."
                ),
                payload={
                    "source_agent": "commercial",
                    "segment_id": segment["id"],
                    "segment_name": segment["name"],
                    "reason": segment["reason"],
                    "channel": segment["channel"],
                    "priority": segment["priority"],
                    "email_count": segment["email_count"],
                    "whatsapp_count": segment["whatsapp_count"],
                    "filters": segment["filters"],
                    "customer_keys": segment["customer_keys"],
                    "company_ids": segment["company_ids"],
                },
                risk_level="medium",
            )
            for segment in report["segments"]
        ]
        return AgentResult(
            summary=(
                f"Cartera unificada: {metrics['customers']} clientes, "
                f"{metrics['contactable']} con canal de contacto y "
                f"{metrics['tiendanube_customers']} vinculados a Climactiva.cl. "
                f"Hay {metrics['customers_at_risk']} clientes en riesgo o inactivos, "
                f"{metrics['omnichannel_customers']} presentes en ambos canales y "
                f"{len(proposals)} segmentos preparados para revision humana."
            ),
            metrics=metrics,
            proposals=proposals,
            evidence=[{"commercial_report": report}],
            warnings=[
                "Los ingresos de Tiendanube no se suman a Facto para evitar doble contabilizacion."
            ],
        )


class MarketingAgent(BusinessAgent):
    async def execute(self, task: HubTask) -> AgentResult:
        snapshot = [
            row for row in task.payload.get("commercial_snapshot", []) if isinstance(row, dict)
        ]
        companies = [
            row for row in task.payload.get("crm_companies", []) if isinstance(row, dict)
        ]
        inventory = [
            row for row in task.payload.get("inventory_snapshot", []) if isinstance(row, dict)
        ]
        if snapshot or companies:
            financial_snapshot = task.payload.get("financial_snapshot")
            commercial_report = build_commercial_report(
                snapshot,
                companies,
                financial_snapshot if isinstance(financial_snapshot, dict) else None,
            )
            business_context = task.payload.get("business_context")
            marketing_report = build_marketing_report(
                commercial_report,
                inventory,
                business_context if isinstance(business_context, dict) else None,
                as_of=task.payload.get("as_of"),
            )
            proposals = [
                ActionProposal(
                    kind=ProposalKind.campaign_draft,
                    title=f"Campaña para revisar: {brief['name']}",
                    summary=(
                        f"{brief['audience']['count']} clientes y "
                        f"{brief['product']['available_units'] if brief.get('product') else 0} "
                        "unidades de stock respaldan este borrador. No se enviará automáticamente."
                    ),
                    payload={
                        "source_agent": "marketing",
                        "campaign_brief_id": brief["id"],
                        "segment_id": brief["audience"]["segment_id"],
                        "segment_name": brief["audience"]["segment_name"],
                        "channel": brief["channel"],
                        "priority": brief["priority"],
                        "customer_keys": brief["audience"]["customer_keys"],
                        "company_ids": brief["audience"]["company_ids"],
                        "product": brief.get("product"),
                        "subject": brief["subject"],
                        "email_body": brief["email_body"],
                        "whatsapp_body": brief["whatsapp_body"],
                        "cta": brief["cta"],
                        "benefit": brief["benefit"],
                    },
                    risk_level="medium",
                )
                for brief in marketing_report["campaign_briefs"]
            ]
            report_metrics = marketing_report["metrics"]
            return AgentResult(
                summary=(
                    f"Plan de marketing preparado con {report_metrics['campaign_briefs']} campañas, "
                    f"{report_metrics['contactable']} clientes contactables y "
                    f"{report_metrics['products_eligible']} productos con stock elegible. "
                    "Todas las propuestas permanecen en borrador hasta revisión humana."
                ),
                metrics=report_metrics,
                proposals=proposals,
                evidence=[{"marketing_report": marketing_report}],
                warnings=[
                    "No se aplicaron descuentos ni beneficios no autorizados.",
                    "Meta WhatsApp debe permanecer deshabilitado hasta completar su aprobación."
                ],
            )

        # Backwards-compatible minimal draft for integrations that still send
        # the original segment/channel payload without synchronized evidence.
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
            if snapshot.get("credit_exposure_available"):
                warnings.append(
                    "Facto permite estimar la exposicion de facturas a credito, pero no entrego el listado de pagos para confirmar la deuda real."
                )
            else:
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
        collections = snapshot.get("collections", {})
        observed_receivables = Decimal(str(collections.get("observed_amount", 0)))
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
                + (
                    f" La cobranza observada suma CLP {observed_receivables:.0f}."
                    if snapshot.get("receivables_available")
                    else (
                        f" La exposicion documental a credito suma CLP {observed_receivables:.0f}, aun sin confirmar pagos."
                        if snapshot.get("credit_exposure_available")
                        else ""
                    )
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
                "collections_observed": float(observed_receivables),
                "collections_overdue": float(collections.get("overdue_amount", 0)),
            },
            evidence=[{"financial_report": snapshot}],
            warnings=warnings,
        )


class CollectionsAgent(BusinessAgent):
    async def execute(self, task: HubTask) -> AgentResult:
        source = str(task.payload.get("source") or "")
        if (
            source not in {"facto_receivables", "facto_document_pdf"}
            or not task.payload.get("authoritative")
        ):
            return AgentResult(
                summary=(
                    "Facto aun no entrego el recurso oficial Cobranza -> Documentos "
                    "impagos ni saldos exactos en sus PDF. El agente no calculara "
                    "deuda desde totales de facturas ni pagos parciales."
                ),
                metrics={
                    "overdue_amount": 0,
                    "official_receivables_available": False,
                    "verified_receivables_available": False,
                },
                warnings=[
                    "Esperando la ruta oficial de solo lectura solicitada a soporte de Facto"
                ],
            )
        overdue = Decimal(str(task.payload.get("overdue_amount", 0)))
        proposal = ActionProposal(
            kind=ProposalKind.collection_reminder,
            title="Recordatorios de cobranza para aprobar",
            summary=f"Cartera vencida informada: {overdue:.2f}. No se enviaran mensajes automaticamente.",
            payload={
                "invoice_ids": task.payload.get("invoice_ids", []),
                "source": source,
            },
            risk_level="high",
        )
        return AgentResult(
            summary=proposal.summary,
            metrics={
                "overdue_amount": float(overdue),
                "official_receivables_available": source == "facto_receivables",
                "verified_receivables_available": True,
            },
            evidence=[
                {
                    "source": source,
                    "documents": task.payload.get("documents", []),
                }
            ],
            proposals=[proposal],
        )


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
        if task.action == "review_import_plan":
            products = [
                row for row in task.payload.get("products", []) if isinstance(row, dict)
            ]
            if not products:
                return AgentResult(
                    summary="No hay inventario Facto disponible para cruzar con el catalogo de importacion.",
                    warnings=[
                        "Sin stock y ventas por SKU no se propone una orden de compra."
                    ],
                )
            raw_as_of = str(task.payload.get("as_of") or date.today().isoformat())
            freight_invoices = [
                row
                for row in task.payload.get("freight_invoices", [])
                if isinstance(row, dict)
            ]
            customs_cost_references = [
                row
                for row in task.payload.get("customs_cost_references", [])
                if isinstance(row, dict)
            ]
            report = build_foreign_trade_report(
                products,
                as_of=date.fromisoformat(raw_as_of),
                freight_invoices=freight_invoices,
                customs_cost_references=customs_cost_references,
            )
            catalog = report["catalog"]
            active_imports = report.get("active_imports", [])
            purchase = report["purchase_proposal"]
            freight_reference = purchase.get("freight_reference") or {}
            customs_reference = report.get("customs_cost_reference", {}).get("summary", {})
            totals = purchase["totals"]
            items = purchase["items"]
            proposals: list[ActionProposal] = []
            if items:
                proposals.append(
                    ActionProposal(
                        kind=ProposalKind.purchase_order,
                        title="Orden Chinafore consolidada para revision",
                        summary=(
                            f"{len(items)} productos por USD {totals['fob_usd']:.2f} FOB, "
                            f"{totals['total_cbm']:.2f} m3 y costo puesto estimado "
                            f"USD {totals['landed_cost_usd']:.2f}."
                        ),
                        payload={
                            "source_agent": "foreign_trade",
                            "supplier": "Chinafore",
                            "purchase_proposal": purchase,
                            "human_approval_required": True,
                        },
                        risk_level="high",
                    )
                )
            summary = (
                f"Se cruzaron {catalog['matched_inventory_products']} productos de Facto "
                f"con {catalog['products']} referencias de importacion; "
                f"{catalog['matched_with_cbm']} coincidencias tienen volumen unitario. "
            )
            if active_imports:
                active_lines = sum(len(item.get("items", [])) for item in active_imports)
                active_fob = sum(
                    float((item.get("totals") or {}).get("fob_usd", 0))
                    for item in active_imports
                )
                summary += (
                    f"Hay {len(active_imports)} importacion activa con {active_lines} partidas "
                    f"y USD {active_fob:.2f} FOB confirmados en produccion. "
                )
            if freight_reference.get("latest_source") == "crm_facto_purchase_invoice":
                summary += (
                    "El flete se valorizo con la factura AD/ADS Cargas "
                    f"{freight_reference.get('latest_invoice_number') or 'sin folio'} del "
                    f"{freight_reference.get('latest_invoice_date') or 'dia sincronizado'}, "
                    "almacenada en el CRM por Facto. "
                )
            summary += (
                "Los demas costos se contrastan con "
                f"{customs_reference.get('verified_documents', 0)} documentos historicos de Agencia "
                "Rodriguez Palma en Gmail; son referencias variables y requieren validacion por despacho. "
            )
            if items:
                summary += (
                    f"La propuesta consolidada contiene {len(items)} productos por "
                    f"USD {totals['fob_usd']:.2f} FOB y {totals['total_cbm']:.2f} m3. "
                    "Queda pendiente de aprobacion humana."
                )
            else:
                summary += (
                    "No se genero una orden porque las coincidencias no requieren reposicion "
                    "con la demanda disponible."
                )
            return AgentResult(
                summary=summary,
                metrics={
                    "catalog_products": int(catalog["products"]),
                    "catalog_with_cbm": int(catalog["with_cbm"]),
                    "matched_inventory_products": int(catalog["matched_inventory_products"]),
                    "matched_with_cbm": int(catalog["matched_with_cbm"]),
                    "proposed_items": len(items),
                    "proposed_fob_usd": float(totals["fob_usd"]),
                    "proposed_landed_cost_usd": float(totals["landed_cost_usd"]),
                    "recoverable_vat_cash_usd": float(
                        totals["recoverable_import_vat_cash_usd"]
                    ),
                    "proposed_cbm": float(totals["total_cbm"]),
                    "container_utilization_percent": float(
                        purchase["container_utilization_percent"]
                    ),
                    "stockout_risks": sum(
                        row["severity"] in {"critical", "high"}
                        for row in report["products"]
                    ),
                    "active_imports": len(active_imports),
                    "active_import_lines": sum(
                        len(item.get("items", [])) for item in active_imports
                    ),
                    "active_import_fob_usd": sum(
                        float((item.get("totals") or {}).get("fob_usd", 0))
                        for item in active_imports
                    ),
                    "freight_invoice_candidates": int(
                        freight_reference.get("crm_invoice_candidates") or 0
                    ),
                    "freight_usable_invoices": int(
                        freight_reference.get("crm_usable_invoices") or 0
                    ),
                    "freight_reference_usd": float(
                        freight_reference.get("latest_verified_usd") or 0
                    ),
                    "customs_reference_documents": int(
                        customs_reference.get("verified_documents") or 0
                    ),
                },
                proposals=proposals,
                evidence=[{"foreign_trade_report": report}],
                warnings=list(purchase["warnings"]),
            )

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
