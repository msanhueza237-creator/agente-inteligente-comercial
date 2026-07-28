from __future__ import annotations

from abc import ABC, abstractmethod

from app.hub.contracts import AgentResult, HubTask


class BusinessAgent(ABC):
    @abstractmethod
    async def execute(self, task: HubTask) -> AgentResult:
        raise NotImplementedError
