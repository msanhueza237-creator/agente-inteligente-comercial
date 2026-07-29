from app.hub.agents import AgentRegistry
from app.hub.contracts import AgentType, HubTask


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
