from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any


HIGH_SEASON_MONTHS = {11, 12, 1, 2}
PRESEASON_MONTHS = {8, 9, 10}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    return int(round(_number(value)))


def _as_of(value: Any) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    return date.today()


def _season(today: date) -> tuple[str, str]:
    if today.month in HIGH_SEASON_MONTHS:
        return "high", "Temporada alta HVAC"
    if today.month in PRESEASON_MONTHS:
        return "preseason", "Preparación de temporada alta"
    return "regular", "Temporada regular"


def _product_candidates(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    candidates: list[dict[str, Any]] = []
    counters = {
        "products_considered": 0,
        "products_eligible": 0,
        "excluded_no_stock": 0,
        "excluded_low_coverage": 0,
    }

    for row in rows:
        sku = str(row.get("sku") or "").strip()
        name = str(row.get("name") or sku).strip()
        if not sku and not name:
            continue
        counters["products_considered"] += 1
        stock_known = bool(row.get("stock_known", row.get("available_units") is not None))
        stock = _number(row.get("available_units"))
        daily_demand = _number(row.get("average_daily_demand"))
        coverage_days = stock / daily_demand if daily_demand > 0 else None
        if not stock_known or stock <= 0:
            counters["excluded_no_stock"] += 1
            continue
        if coverage_days is not None and coverage_days < 45:
            counters["excluded_low_coverage"] += 1
            continue
        if coverage_days is None and stock < 10:
            counters["excluded_low_coverage"] += 1
            continue

        sold_units = _number(row.get("units_sold_observed"))
        sales_revenue = _number(row.get("sales_revenue_observed"))
        net_price = _number(row.get("unit_price"))
        score = sold_units * 8 + min(stock, 500) + sales_revenue / 100_000
        if sold_units <= 0:
            # A product without observed sales can still be useful for a slow-stock
            # campaign, but it must rank after products with demonstrated demand.
            score = min(stock, 500) / 4
        candidates.append(
            {
                "sku": sku,
                "name": name,
                "available_units": _integer(stock),
                "average_daily_demand": round(daily_demand, 2),
                "coverage_days": round(coverage_days, 1) if coverage_days is not None else None,
                "units_sold_observed": _integer(sold_units),
                "sales_revenue_observed": round(sales_revenue),
                "net_unit_price": round(net_price),
                "price_is_net": bool(row.get("unit_price_is_net", True)),
                "has_observed_sales": bool(row.get("has_observed_sales", sold_units > 0)),
                "last_sale_at": row.get("last_sale_at"),
                "score": round(score, 2),
                "source": str(row.get("source") or "facto"),
            }
        )

    candidates.sort(
        key=lambda row: (
            bool(row["has_observed_sales"]),
            row["score"],
            row["available_units"],
        ),
        reverse=True,
    )
    counters["products_eligible"] = len(candidates)
    return candidates, counters


def _copy_for_segment(
    segment: dict[str, Any],
    product: dict[str, Any] | None,
    approved_benefits: dict[str, str],
) -> dict[str, str]:
    segment_id = str(segment.get("id") or "segment")
    product_name = product["name"] if product else "soluciones HVAC disponibles"
    benefit = approved_benefits.get(segment_id, "").strip()

    content: dict[str, tuple[str, str, str]] = {
        "valuable_customers_to_rescue": (
            "Queremos volver a trabajar contigo",
            "Recuperar clientes valiosos con una conversación comercial personal.",
            "Hola {{nombre_contacto}}, queremos retomar el contacto con {{nombre_empresa}}. "
            f"Hoy contamos con disponibilidad de {product_name}. ¿Podemos preparar una propuesta para sus próximos proyectos?",
        ),
        "web_customers_to_develop": (
            "Gracias por comprar en Climactiva.cl",
            "Acompañar compradores web para desarrollar una relación comercial recurrente.",
            "Hola {{nombre_contacto}}, gracias por comprar en Climactiva.cl. "
            f"También podemos apoyarte directamente con disponibilidad y asesoría sobre {product_name}. ¿Te gustaría recibir atención comercial?",
        ),
        "loyal_customers_cross_sell": (
            "Una solución complementaria para tus próximas compras",
            "Ampliar el mix de clientes recurrentes con productos respaldados por stock.",
            "Hola {{nombre_contacto}}, revisando las compras de {{nombre_empresa}} identificamos una alternativa complementaria: "
            f"{product_name}. ¿Quieres que preparemos disponibilidad y una propuesta?",
        ),
        "new_customer_onboarding": (
            "Bienvenido a Clima Activa",
            "Acompañar la segunda compra y explicar los canales de atención disponibles.",
            "Hola {{nombre_contacto}}, gracias por confiar en Clima Activa. "
            f"Para tu próxima compra podemos ayudarte con {product_name} y atención especializada. ¿Necesitas disponibilidad o asesoría?",
        ),
        "dormant_customers": (
            "Novedades y stock para tus proyectos HVAC",
            "Reactivar clientes inactivos sin asumir que todavía existe interés.",
            "Hola {{nombre_contacto}}, hace tiempo que no conversamos con {{nombre_empresa}}. "
            f"Tenemos disponibilidad de {product_name}. Si sigue siendo relevante para ustedes, podemos enviar una propuesta actualizada.",
        ),
        "at_risk_customers": (
            "¿Podemos ayudarte con tu próxima compra?",
            "Detectar necesidades de clientes cuya frecuencia de compra disminuyó.",
            "Hola {{nombre_contacto}}, queremos saber si {{nombre_empresa}} necesita apoyo para sus próximos trabajos. "
            f"Contamos con {product_name}. ¿Hay algún producto o proyecto que debamos cotizar?",
        ),
        "hvac_technicians": (
            "Stock y apoyo para técnicos e instaladores",
            "Conectar técnicos e instaladores con productos disponibles para sus trabajos.",
            "Hola {{nombre_contacto}}, desde Clima Activa queremos apoyarte con stock para tus trabajos de climatización. "
            f"Hoy tenemos disponibilidad de {product_name}. ¿Quieres que revisemos precio neto y despacho?",
        ),
        "hvac_distribution": (
            "Propuesta comercial para distribuidores HVAC",
            "Presentar stock y oportunidades de compra por volumen a distribuidores y tiendas.",
            "Hola {{nombre_contacto}}, queremos presentar a {{nombre_empresa}} una propuesta comercial con stock disponible. "
            f"Una de las oportunidades actuales es {product_name}. ¿Podemos preparar condiciones según volumen?",
        ),
    }
    subject, objective, body = content.get(
        segment_id,
        (
            str(segment.get("name") or "Propuesta Clima Activa"),
            str(segment.get("reason") or "Desarrollar una oportunidad comercial trazable."),
            f"Hola {{nombre_contacto}}, tenemos disponibilidad de {product_name}. ¿Podemos enviarte una propuesta?",
        ),
    )
    if benefit:
        body = f"{body}\n\nBeneficio autorizado: {benefit}"
    return {
        "subject": subject,
        "objective": objective,
        "email_body": f"{body}\n\nSaludos,\nEquipo Clima Activa",
        "whatsapp_body": body,
        "cta": "Solicitar propuesta o confirmar interés",
    }


def build_marketing_report(
    commercial_report: dict[str, Any],
    inventory_rows: list[dict[str, Any]],
    business_context: dict[str, Any] | None = None,
    *,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    """Turn commercial evidence into reviewable campaign briefs.

    The function never sends messages, invents discounts or recommends a
    product without stock evidence.  It intentionally keeps customer keys in
    every brief so the CRM can resolve recipients and require human approval.
    """

    context = business_context or {}
    today = _as_of(as_of or context.get("as_of"))
    season_id, season_label = _season(today)
    candidates, product_metrics = _product_candidates(inventory_rows)
    fast_products = [row for row in candidates if row["has_observed_sales"]]
    slow_products = [row for row in candidates if not row["has_observed_sales"]]
    approved_benefits_value = context.get("approved_benefits")
    approved_benefits = (
        approved_benefits_value
        if isinstance(approved_benefits_value, dict)
        else {}
    )

    briefs: list[dict[str, Any]] = []
    segments = [row for row in commercial_report.get("segments", []) if isinstance(row, dict)]
    for index, segment in enumerate(segments):
        count = _integer(segment.get("count"))
        if count <= 0:
            continue
        segment_id = str(segment.get("id") or f"segment-{index}")
        prefer_slow = segment_id in {"loyal_customers_cross_sell", "dormant_customers"}
        pool = slow_products if prefer_slow and slow_products else fast_products or candidates
        product = pool[index % len(pool)] if pool else None
        copy = _copy_for_segment(segment, product, approved_benefits)
        evidence = [
            f"Segmento comercial {segment_id}: {count} clientes",
            f"Canales disponibles: {int(segment.get('email_count') or 0)} email y {int(segment.get('whatsapp_count') or 0)} WhatsApp",
        ]
        if product:
            evidence.append(
                f"Facto: {product['available_units']} unidades disponibles de {product['sku']}"
            )
            if product["has_observed_sales"]:
                evidence.append(
                    f"Ventas observadas: {product['units_sold_observed']} unidades"
                )
        briefs.append(
            {
                "id": f"marketing-{segment_id}",
                "name": str(segment.get("name") or "Campaña dirigida"),
                "objective": copy["objective"],
                "reason": str(segment.get("reason") or ""),
                "priority": str(segment.get("priority") or "medium"),
                "channel": str(segment.get("channel") or "email"),
                "status": "draft",
                "audience": {
                    "segment_id": segment_id,
                    "segment_name": str(segment.get("name") or "Segmento"),
                    "count": count,
                    "email_count": _integer(segment.get("email_count")),
                    "whatsapp_count": _integer(segment.get("whatsapp_count")),
                    "filters": segment.get("filters") or {},
                    "customer_keys": segment.get("customer_keys") or [],
                    "company_ids": segment.get("company_ids") or [],
                },
                "product": product,
                "subject": copy["subject"],
                "email_body": copy["email_body"],
                "whatsapp_body": copy["whatsapp_body"],
                "cta": copy["cta"],
                "benefit": approved_benefits.get(segment_id, ""),
                "measurement": [
                    "destinatarios confirmados",
                    "mensajes enviados",
                    "respuestas",
                    "interesados",
                    "conversiones verificadas",
                ],
                "requires_approval": True,
                "evidence": evidence,
            }
        )

    metrics = commercial_report.get("metrics", {})
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "strategy": {
            "as_of": today.isoformat(),
            "season": season_id,
            "season_label": season_label,
            "high_season_months": [11, 12, 1, 2],
            "automatic_sending": False,
            "human_approval_required": True,
        },
        "metrics": {
            "customers": _integer(metrics.get("customers")),
            "contactable": _integer(metrics.get("contactable")),
            "email_ready": _integer(metrics.get("email_ready")),
            "whatsapp_ready": _integer(metrics.get("whatsapp_ready")),
            "audiences": len(segments),
            "campaign_briefs": len(briefs),
            **product_metrics,
        },
        "customers": commercial_report.get("customers", []),
        "audiences": segments,
        "campaign_briefs": briefs,
        "product_opportunities": candidates[:20],
        "guardrails": [
            "Ninguna campaña se envía automáticamente.",
            "Cada destinatario debe ser revisado en Campañas antes del envío.",
            "No se inventan descuentos, cupones ni beneficios.",
            "Sólo se recomiendan productos con stock y cobertura comercial suficiente.",
            "WhatsApp respeta consentimiento y estado de contacto del CRM.",
        ],
        "methodology": (
            "La audiencia proviene de la cartera unificada Facto, Climactiva.cl y CRM. "
            "Los productos provienen del inventario y ventas observadas en Facto. "
            "Las propuestas son borradores sujetos a aprobación humana."
        ),
    }
