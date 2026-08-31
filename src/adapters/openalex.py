"""Small OpenAlex Works adapter for Archive Discovery."""

from datetime import datetime

import httpx

from ..core.models import normalize_doi


class OpenAlexAdapter:
    BASE_URL = "https://api.openalex.org/works"

    def __init__(self, email: str | None = None, timeout: float = 30.0):
        self.email = email
        self._client = httpx.AsyncClient(timeout=timeout)

    async def search_works(
        self,
        *,
        query: str,
        cursor: str = "*",
        rows: int = 50,
    ) -> tuple[list[dict], str | None]:
        params = {
            "search": query,
            "cursor": cursor,
            "per-page": str(min(rows, 200)),
        }
        if self.email:
            params["mailto"] = self.email
        response = await self._client.get(self.BASE_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("results", [])
        next_cursor = payload.get("meta", {}).get("next_cursor")
        return items, next_cursor if items and next_cursor else None

    async def close(self) -> None:
        await self._client.aclose()


def openalex_external_id(raw: dict) -> str:
    return str(raw.get("id", "")).rstrip("/").rsplit("/", 1)[-1]


def _abstract_from_inverted_index(index: dict | None) -> str | None:
    if not index:
        return None
    positioned = [(position, word) for word, positions in index.items() for position in positions]
    return " ".join(word for _, word in sorted(positioned)) or None


def parse_openalex_metadata(raw: dict) -> dict:
    date_text = raw.get("publication_date")
    publication_date = None
    raw_parts = None
    if date_text:
        try:
            publication_date = datetime.fromisoformat(date_text)
            raw_parts = [publication_date.year, publication_date.month, publication_date.day]
        except ValueError:
            publication_date = None
    authors = []
    for authorship in raw.get("authorships", []):
        author = authorship.get("author") or {}
        institutions = authorship.get("institutions") or []
        authors.append(
            {
                "name": author.get("display_name", ""),
                "affiliation": [item.get("display_name", "") for item in institutions],
                "orcid": author.get("orcid"),
            }
        )
    primary_location = raw.get("primary_location") or {}
    source = primary_location.get("source") or {}
    ids = raw.get("ids") or {}
    pmid = str(ids.get("pmid", "")).rstrip("/").rsplit("/", 1)[-1]
    external_id = openalex_external_id(raw)
    other_ids = {
        "openalex_id": external_id,
        "openalex_type": raw.get("type"),
        "cited_by_count": raw.get("cited_by_count", 0),
    }
    if pmid:
        other_ids["pmid"] = pmid
    return {
        "title": raw.get("display_name") or raw.get("title") or "",
        "authors": authors,
        "journal": source.get("display_name") or None,
        "publication_date": publication_date,
        "publication_date_precision": "day" if publication_date else None,
        "raw_date_parts": raw_parts,
        "doi": normalize_doi(raw["doi"]) if raw.get("doi") else None,
        "abstract": _abstract_from_inverted_index(raw.get("abstract_inverted_index")),
        "other_ids": other_ids,
        "publication_status": "ACTIVE",
        "raw": raw,
    }
