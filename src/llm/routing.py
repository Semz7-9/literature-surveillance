"""Role-based model routing contracts for Archive automation.

The workflow asks for a capability role. Provider-specific adapters can be
registered later without teaching ArchiveBuildRun about vendor names.
"""

from dataclasses import dataclass, field
from typing import Protocol

AI_ROLES = {"SOURCE_SYNTHESIS", "REASONING", "CRITIQUE"}


@dataclass(frozen=True)
class AIContributionRequest:
    role: str
    prompt: str
    input_refs: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class AIContributionResult:
    provider: str
    model: str
    role: str
    output: dict


class RoleProvider(Protocol):
    async def contribute(self, request: AIContributionRequest) -> AIContributionResult: ...


class RoleBasedModelRouter:
    """Small capability registry; intentionally not an Agent framework."""

    def __init__(self) -> None:
        self._providers: dict[str, RoleProvider] = {}

    def register(self, role: str, provider: RoleProvider) -> None:
        if role not in AI_ROLES:
            raise ValueError(f"不支持的 AI role：{role}")
        self._providers[role] = provider

    async def contribute(self, request: AIContributionRequest) -> AIContributionResult:
        if request.role not in AI_ROLES:
            raise ValueError(f"不支持的 AI role：{request.role}")
        provider = self._providers.get(request.role)
        if provider is None:
            raise LookupError(f"尚未配置 {request.role} provider")
        return await provider.contribute(request)
