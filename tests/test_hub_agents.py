from app.hub.agents import AgentRegistry
from app.hub.contracts import AgentType, ClaimedHubTask, HubTask


def test_claim_contract_accepts_only_domain_fields() -> None:
    database_row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "agent_type": "foreign_trade",
        "action": "analyze_inventory",
        "payload": {},
        "requested_by": None,
        "created_at": "2026-07-29T12:00:00+00:00",
        "status": "in_progress",
        "attempts": 1,
        "priority": 50,
        "worker_id": "climactiva-hub-01",
        "result": None,
    }
    domain_row = {
        field: database_row.get(field)
        for field in ("id", "agent_type", "action", "payload", "requested_by", "created_at")
    }
    claimed = ClaimedHubTask.model_validate(
        {
            **domain_row,
            "lease_token": "00000000-0000-0000-0000-000000000002",
            "lease_expires_at": "2026-07-29T12:02:00+00:00",
        }
    )
    assert claimed.agent_type is AgentType.foreign_trade


async def test_marketing_agent_only_creates_approval_proposal() -> None:
    result = await AgentRegistry().get(AgentType.marketing).execute(
        HubTask(
            id="task-1",
            agent_type=AgentType.marketing,
            action="draft_campaign",
            payload={"segment": "tecnicos", "channel": "email"},
        )
    )
    assert len(result.proposals) == 1
    assert result.proposals[0].requires_approval is True


async def test_executive_morning_brief_is_always_deliverable() -> None:
    result = await AgentRegistry().get(AgentType.executive).execute(
        HubTask(
            id="task-executive-morning",
            agent_type=AgentType.executive,
            action="analyze_company",
            payload={"mode": "morning", "signals": {}},
        )
    )

    assert result.metrics["notification_required"] is True
    assert result.metrics["is_morning"] is True
    assert result.metrics["alerts"] == 0
    assert result.evidence[0]["executive_brief"]["mode"] == "morning"


async def test_executive_review_only_notifies_for_relevant_signals() -> None:
    quiet = await AgentRegistry().get(AgentType.executive).execute(
        HubTask(
            id="task-executive-quiet",
            agent_type=AgentType.executive,
            action="analyze_company",
            payload={"mode": "review", "signals": {}},
        )
    )
    relevant = await AgentRegistry().get(AgentType.executive).execute(
        HubTask(
            id="task-executive-relevant",
            agent_type=AgentType.executive,
            action="analyze_company",
            payload={
                "mode": "review",
                "signals": {
                    "sales": [{"folio": "1001", "total": 119000}],
                    "campaign_replies": [{"from": "+56911111111", "message": "Me interesa"}],
                },
            },
        )
    )

    assert quiet.metrics["notification_required"] is False
    assert quiet.proposals == []
    assert relevant.metrics["notification_required"] is True
    assert relevant.metrics["new_sales"] == 1
    assert relevant.metrics["campaign_replies"] == 1
    assert relevant.proposals[0].requires_approval is True


async def test_finance_agent_uses_facto_snapshot_and_marks_missing_sources() -> None:
    result = await AgentRegistry().get(AgentType.finance).execute(
        HubTask(
            id="task-finance",
            agent_type=AgentType.finance,
            action="review_margin",
            payload={
                "financial_snapshot": {
                    "document_count": 2,
                    "net_sales": 150000,
                    "tax": 28500,
                    "gross_sales": 178500,
                    "average_net_ticket": 75000,
                    "reference_cost_of_sales": 60000,
                    "reference_margin_available": True,
                    "receivables_available": False,
                    "expenses_available": False,
                }
            },
        )
    )

    assert result.metrics["net_sales"] == 150000
    assert result.metrics["reference_margin"] == 90000
    assert len(result.warnings) == 3
    assert result.evidence[0]["financial_report"]["document_count"] == 2


async def test_collections_agent_refuses_inferred_debt() -> None:
    result = await AgentRegistry().get(AgentType.collections).execute(
        HubTask(
            id="task-collections-unavailable",
            agent_type=AgentType.collections,
            action="review_aging",
            payload={
                "source": "registered_payments",
                "authoritative": True,
                "overdue_amount": 999999,
                "invoice_ids": ["INFERRED-1"],
            },
        )
    )

    assert result.metrics["overdue_amount"] == 0
    assert result.metrics["official_receivables_available"] is False
    assert result.proposals == []
    assert result.warnings


async def test_collections_agent_uses_only_official_facto_receivables() -> None:
    result = await AgentRegistry().get(AgentType.collections).execute(
        HubTask(
            id="task-collections-official",
            agent_type=AgentType.collections,
            action="review_aging",
            payload={
                "source": "facto_receivables",
                "authoritative": True,
                "overdue_amount": 125000,
                "invoice_ids": ["FACTO-1"],
                "documents": [{"document_id": "FACTO-1", "observed_amount": 125000}],
            },
        )
    )

    assert result.metrics["overdue_amount"] == 125000
    assert result.metrics["official_receivables_available"] is True
    assert len(result.proposals) == 1
    assert result.proposals[0].requires_approval is True
    assert result.evidence[0]["source"] == "facto_receivables"


async def test_collections_agent_accepts_exact_facto_pdf_balance() -> None:
    result = await AgentRegistry().get(AgentType.collections).execute(
        HubTask(
            id="task-collections-pdf",
            agent_type=AgentType.collections,
            action="review_aging",
            payload={
                "source": "facto_document_pdf",
                "authoritative": True,
                "overdue_amount": 3177222,
                "invoice_ids": ["INV-PDF-1"],
                "documents": [
                    {"document_id": "INV-PDF-1", "observed_amount": 3177222}
                ],
            },
        )
    )

    assert result.metrics["overdue_amount"] == 3177222
    assert result.metrics["official_receivables_available"] is False
    assert result.metrics["verified_receivables_available"] is True
    assert len(result.proposals) == 1
    assert result.evidence[0]["source"] == "facto_document_pdf"


async def test_all_six_agents_are_registered() -> None:
    assert set(AgentRegistry().names()) == {agent.value for agent in AgentType}


async def test_foreign_trade_agent_keeps_inventory_evidence_for_crm_review() -> None:
    result = await AgentRegistry().get(AgentType.foreign_trade).execute(
        HubTask(
            id="task-inventory",
            agent_type=AgentType.foreign_trade,
            action="review_inventory_risk",
            payload={
                "sku": "AC-01",
                "available_units": 3,
                "average_daily_demand": 1,
                "unit_cost_usd": 100,
                "as_of": "2026-07-10",
            },
        )
    )

    evidence = result.evidence[0]["inventory_recommendation"]
    assert evidence["sku"] == "AC-01"
    assert evidence["recommended_units"] > 0
    assert result.proposals[0].requires_approval is True


async def test_foreign_trade_agent_reports_missing_inventory_evidence() -> None:
    result = await AgentRegistry().get(AgentType.foreign_trade).execute(
        HubTask(
            id="task-inventory-readiness",
            agent_type=AgentType.foreign_trade,
            action="review_inventory_readiness",
            payload={
                "catalog_count": 25,
                "stock_known": 25,
                "cost_known": 0,
                "cost_available_in_source": 25,
                "cost_requires_usd_conversion": 25,
                "demand_available": 0,
                "eligible": 0,
            },
        )
    )

    assert result.metrics["catalog_count"] == 25
    assert result.metrics["eligible"] == 0
    assert result.proposals == []
    assert "no inventa datos" in result.summary
    assert "25 con stock" in result.summary
    assert result.metrics["cost_available_in_source"] == 25
    assert result.warnings


async def test_logistics_agent_prepares_cross_agent_proposals() -> None:
    result = await AgentRegistry().get(AgentType.logistics).execute(
        HubTask(
            id="task-logistics",
            agent_type=AgentType.logistics,
            action="review_logistics",
            payload={
                "products": [
                    {
                        "sku": "FAST-01",
                        "name": "Alta rotacion",
                        "stock_known": True,
                        "available_units": 5,
                        "sales_history_available": True,
                        "average_daily_demand": 1,
                        "units_sold_observed": 30,
                        "cost_known": True,
                        "price_known": True,
                        "margin_percent": 35,
                    },
                    {
                        "sku": "SLOW-01",
                        "name": "Sin movimiento",
                        "stock_known": True,
                        "available_units": 12,
                        "sales_history_available": True,
                        "average_daily_demand": 0,
                        "units_sold_observed": 0,
                    },
                ]
            },
        )
    )

    assert result.metrics["slow_moving"] == 1
    assert result.metrics["high_rotation_low_stock"] == 1
    assert {proposal.kind.value for proposal in result.proposals} == {
        "campaign_draft",
        "executive_alert",
    }
