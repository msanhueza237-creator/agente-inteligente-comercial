from __future__ import annotations

from app.hub.agents.base import BusinessAgent
from app.hub.agents.business import (
    CollectionsAgent,
    CommercialAgent,
    ExecutiveAgent,
    FinanceAgent,
    ForeignTradeAgent,
    LogisticsAgent,
    MarketingAgent,
)
from app.hub.contracts import AgentType


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[AgentType, BusinessAgent] = {
            AgentType.commercial: CommercialAgent(),
            AgentType.marketing: MarketingAgent(),
            AgentType.finance: FinanceAgent(),
            AgentType.collections: CollectionsAgent(),
            AgentType.logistics: LogisticsAgent(),
            AgentType.foreign_trade: ForeignTradeAgent(),
            AgentType.executive: ExecutiveAgent(),
        }

    def get(self, agent_type: AgentType) -> BusinessAgent:
        return self._agents[agent_type]

    def names(self) -> list[str]:
        return [agent_type.value for agent_type in self._agents]
