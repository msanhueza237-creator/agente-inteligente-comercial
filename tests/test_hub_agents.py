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
