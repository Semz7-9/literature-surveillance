"""Deterministic compilers from an EffectiveSearchPlan to provider requests."""

from typing import Protocol

from ..core.models import EffectiveSearchPlan


class QueryCompiler(Protocol):
    provider: str

    def compile(self, plan: EffectiveSearchPlan) -> list[dict]: ...


def _terms(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.replace('"', "").strip() for value in values if value.strip()))


def _pubmed_group(values: list[str]) -> str:
    return "(" + " OR ".join(f'"{term}"[Title/Abstract]' for term in _terms(values)) + ")"


class PubMedCompiler:
    provider = "pubmed"

    def compile(self, plan: EffectiveSearchPlan) -> list[dict]:
        concepts = [item for item in plan.concepts if _terms(item.get("terms", []))]
        if not concepts:
            raise ValueError("EffectiveSearchPlan has no searchable concepts")
        anchor_terms = _terms([*concepts[0]["terms"], *plan.historical_vocabulary])
        anchor = _pubmed_group(anchor_terms)
        exclusions = _terms(plan.hard_exclusions)
        suffix = f" NOT {_pubmed_group(exclusions)}" if exclusions else ""
        queries = [
            {
                "query_id": "P1",
                "query": f"{anchor}{suffix}",
                "reason": "Core concept plus historical vocabulary",
            }
        ]
        for index, concept in enumerate(concepts[1:], 2):
            queries.append(
                {
                    "query_id": f"P{index}",
                    "query": f"{anchor} AND {_pubmed_group(concept['terms'])}{suffix}",
                    "reason": f"Core concept crossed with {concept.get('label', 'branch')}",
                }
            )
        return queries


class OpenAlexCompiler:
    provider = "openalex"

    def compile(self, plan: EffectiveSearchPlan) -> list[dict]:
        concepts = [item for item in plan.concepts if _terms(item.get("terms", []))]
        if not concepts:
            raise ValueError("EffectiveSearchPlan has no searchable concepts")
        anchor_terms = _terms([*concepts[0]["terms"], *plan.historical_vocabulary])
        queries = [
            {
                "query_id": "O1",
                "query": " OR ".join(f'"{term}"' for term in anchor_terms),
                "reason": "Core concept plus historical vocabulary",
            }
        ]
        anchor = concepts[0]["terms"][0]
        for index, concept in enumerate(concepts[1:], 2):
            branch = " OR ".join(f'"{term}"' for term in _terms(concept["terms"]))
            queries.append(
                {
                    "query_id": f"O{index}",
                    "query": f'"{anchor}" {branch}',
                    "reason": f"Core concept crossed with {concept.get('label', 'branch')}",
                }
            )
        return queries


def compile_queries(plan: EffectiveSearchPlan) -> dict[str, list[dict]]:
    return {
        compiler.provider: compiler.compile(plan)
        for compiler in (PubMedCompiler(), OpenAlexCompiler())
        if compiler.provider in plan.source_targets
    }
