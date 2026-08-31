"""PubMed E-utilities adapter for topic and journal monitoring."""

from datetime import datetime
from xml.etree import ElementTree

import httpx

from ..core.models import normalize_doi


class PubMedAdapter:
    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, email: str, api_key: str | None = None, timeout: float = 30.0):
        self.email = email
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    def _common_params(self) -> dict[str, str]:
        params = {"tool": "literature-surveillance", "email": self.email}
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    async def discover_works(
        self,
        *,
        from_date: str,
        until_date: str,
        issn: str | None = None,
        query: str | None = None,
        feed_mode: str = "created",
        cursor: str = "*",
        rows: int = 100,
        sort: str = "pub date",
    ) -> tuple[list[dict], str | None]:
        """Return one stable PubMed result page including abstracts and MeSH terms."""
        del feed_mode
        if not issn and not query:
            raise ValueError("PubMed discovery requires an ISSN or a query")
        search = query.strip() if query else f'"{issn.strip()}"[ISSN]'
        term = f"({search}) AND ({from_date.replace('-', '/')}:{until_date.replace('-', '/')}[CRDT])"
        offset = 0 if cursor in {"", "*", None} else int(cursor)
        params = {
            **self._common_params(), "db": "pubmed", "retmode": "json",
            "sort": sort, "term": term, "retstart": str(offset),
            "retmax": str(min(rows, 200)),
        }
        response = await self._client.get(f"{self.BASE_URL}/esearch.fcgi", params=params)
        response.raise_for_status()
        search_result = response.json()["esearchresult"]
        ids = search_result.get("idlist", [])
        total = int(search_result.get("count", 0))
        if not ids:
            return [], None
        fetch = await self._client.get(
            f"{self.BASE_URL}/efetch.fcgi",
            params={
                **self._common_params(), "db": "pubmed", "retmode": "xml",
                "id": ",".join(ids),
            },
        )
        fetch.raise_for_status()
        root = ElementTree.fromstring(fetch.content)
        items = [_article_to_metadata(article) for article in root.findall(".//PubmedArticle")]
        next_offset = offset + len(ids)
        return items, str(next_offset) if next_offset < total else None

    async def close(self) -> None:
        await self._client.aclose()


def _text(element) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def _article_to_metadata(article) -> dict:
    citation = article.find("MedlineCitation")
    journal_article = citation.find("Article")
    pmid = _text(citation.find("PMID"))
    doi = None
    pmcid = None
    for identifier in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        kind = identifier.attrib.get("IdType")
        if kind == "doi":
            doi = normalize_doi(_text(identifier))
        elif kind == "pmc":
            pmcid = _text(identifier)
    authors = []
    for author in journal_article.findall(".//AuthorList/Author"):
        name = " ".join(filter(None, [
            _text(author.find("ForeName")), _text(author.find("LastName")),
        ])) or _text(author.find("CollectiveName"))
        authors.append({"name": name, "affiliation": [], "orcid": None})
    date = journal_article.find(".//Journal/JournalIssue/PubDate")
    year_text = _text(date.find("Year")) if date is not None else ""
    month_text = _text(date.find("Month")) if date is not None else ""
    day_text = _text(date.find("Day")) if date is not None else ""
    mesh = [_text(node.find("DescriptorName")) for node in citation.findall(".//MeshHeading")]
    return {
        "PMID": pmid,
        "DOI": doi,
        "title": _text(journal_article.find("ArticleTitle")),
        "authors": authors,
        "journal": _text(journal_article.find(".//Journal/Title")),
        "date": {"year": year_text, "month": month_text, "day": day_text},
        "abstract": "\n".join(
            filter(None, [_text(node) for node in journal_article.findall(".//Abstract/AbstractText")])
        ) or None,
        "pmcid": pmcid,
        "mesh": list(filter(None, mesh)),
    }


def parse_pubmed_metadata(raw: dict) -> dict:
    date = raw.get("date", {})
    year = int(date["year"]) if str(date.get("year", "")).isdigit() else None
    month_names = {
        name: index for index, name in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
        )
    }
    month_raw = str(date.get("month", ""))[:3].title()
    month = int(month_raw) if month_raw.isdigit() else month_names.get(month_raw)
    day = int(date["day"]) if str(date.get("day", "")).isdigit() else None
    publication_date = datetime(year, month or 1, day or 1) if year else None
    precision = "day" if year and month and day else "month" if year and month else "year" if year else None
    raw_parts = [part for part in [year, month, day] if part is not None] or None
    other_ids = {"pmid": raw["PMID"]}
    if raw.get("pmcid"):
        other_ids["pmcid"] = raw["pmcid"]
    if raw.get("mesh"):
        other_ids["mesh"] = raw["mesh"]
    return {
        "title": raw.get("title", ""), "authors": raw.get("authors", []),
        "journal": raw.get("journal") or None, "publication_date": publication_date,
        "publication_date_precision": precision, "raw_date_parts": raw_parts,
        "doi": normalize_doi(raw["DOI"]) if raw.get("DOI") else None,
        "abstract": raw.get("abstract"), "other_ids": other_ids,
        "publication_status": "ACTIVE", "raw": raw,
    }
