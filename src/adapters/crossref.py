"""
Crossref Adapter

从 Crossref REST API 获取文献元数据

限速：
- Polite pool: 10 req/s, concurrent 3 (single record)
- List queries: 3 req/s, concurrent 3
- 需要在 User-Agent 中包含邮箱
"""

import asyncio
from datetime import datetime
from typing import Optional
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from ..core.models import normalize_doi


class CrossrefAdapter:
    """Crossref API 适配器"""

    BASE_URL = "https://api.crossref.org"

    def __init__(self, email: str, rate_limit: float = 10.0, timeout: float = 30.0):
        """
        Args:
            email: 用于 Polite Pool 的邮箱
            rate_limit: 每秒请求数（默认 10）
            timeout: 请求超时（秒）
        """
        self.email = email
        self.rate_limit = rate_limit
        self.timeout = timeout

        # Rate limiter
        self._last_request_time = 0.0
        self._lock = asyncio.Lock()

        # HTTP client
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": f"LiteratureAssistant/0.1 (mailto:{email})",
            },
            timeout=timeout,
        )

    async def _wait_for_rate_limit(self):
        """等待满足限速要求"""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            min_interval = 1.0 / self.rate_limit
            time_since_last = now - self._last_request_time

            if time_since_last < min_interval:
                await asyncio.sleep(min_interval - time_since_last)

            self._last_request_time = asyncio.get_event_loop().time()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    )
    async def get_work_by_doi(self, doi: str) -> dict:
        """
        通过 DOI 获取 work 元数据

        Args:
            doi: DOI (可以带或不带 'https://doi.org/' 前缀)

        Returns:
            Crossref work metadata

        Raises:
            httpx.HTTPStatusError: 如果 DOI 不存在或 API 错误
        """
        # 规范化 DOI（大小写、URL 前缀、空白）
        doi = normalize_doi(doi)

        await self._wait_for_rate_limit()

        url = f"{self.BASE_URL}/works/{doi}"
        response = await self._client.get(url)
        response.raise_for_status()

        data = response.json()
        return data["message"]

    async def get_relations(self, doi: str) -> dict:
        """
        获取 DOI 的关系信息（is-preprint-of, is-version-of, etc.）

        Returns:
            包含 relation 字段的字典
        """
        work = await self.get_work_by_doi(doi)
        return work.get("relation", {})

    async def discover_works(
        self,
        *,
        from_date: str,
        until_date: str,
        issn: str | None = None,
        query: str | None = None,
        cursor: str = "*",
        rows: int = 100,
    ) -> tuple[list[dict], str | None]:
        """Discover a deterministic page of recently updated Crossref works."""
        if not issn and not query:
            raise ValueError("Crossref discovery requires an ISSN or a query")
        await self._wait_for_rate_limit()
        filters = [f"from-update-date:{from_date}", f"until-update-date:{until_date}"]
        if issn:
            filters.append(f"issn:{issn.strip()}")
        params = {
            "filter": ",".join(filters),
            "cursor": cursor,
            "rows": min(rows, 1000),
            "sort": "updated",
            "order": "asc",
        }
        if query:
            params["query.bibliographic"] = query.strip()
        response = await self._client.get(f"{self.BASE_URL}/works", params=params)
        response.raise_for_status()
        message = response.json()["message"]
        return message.get("items", []), message.get("next-cursor")

    async def close(self):
        """关闭 HTTP client"""
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


def parse_crossref_metadata(crossref_work: dict) -> dict:
    """
    将 Crossref work 转换为标准化的元数据格式

    Args:
        crossref_work: Crossref API 返回的 message 字段

    Returns:
        标准化元数据
    """
    # 标题
    title = ""
    if "title" in crossref_work and crossref_work["title"]:
        title = crossref_work["title"][0]

    # 作者
    authors = []
    for author in crossref_work.get("author", []):
        name_parts = []
        if "given" in author:
            name_parts.append(author["given"])
        if "family" in author:
            name_parts.append(author["family"])

        authors.append({
            "name": " ".join(name_parts),
            "affiliation": author.get("affiliation", []),
            "orcid": author.get("ORCID", None),
        })

    # 期刊
    journal = None
    if "container-title" in crossref_work and crossref_work["container-title"]:
        journal = crossref_work["container-title"][0]

    # 日期
    # Crossref 的 date-parts 精度不一致（可能只给年，或年+月）。
    # publication_date 是按精度补全到该单位第一天的派生值，仅用于排序/展示；
    # raw_date_parts + publication_date_precision 保留真实精度，不能让
    # "2024" 悄悄变成看起来像确切知道的"2024-01-01"。
    publication_date = None
    publication_date_precision = None
    raw_date_parts = None
    if "published" in crossref_work:
        raw_date_parts = crossref_work["published"].get("date-parts", [[]])[0]
        if len(raw_date_parts) >= 3:
            publication_date = datetime(raw_date_parts[0], raw_date_parts[1], raw_date_parts[2])
            publication_date_precision = "day"
        elif len(raw_date_parts) == 2:
            publication_date = datetime(raw_date_parts[0], raw_date_parts[1], 1)
            publication_date_precision = "month"
        elif len(raw_date_parts) == 1:
            publication_date = datetime(raw_date_parts[0], 1, 1)
            publication_date_precision = "year"

    # DOI（规范化，避免大小写/URL 前缀造成的重复实体）
    doi = crossref_work.get("DOI")
    if doi:
        doi = normalize_doi(doi)

    # Abstract (如果有)
    abstract = crossref_work.get("abstract")

    # 其他标识符
    other_ids = {}
    if "PMID" in crossref_work:
        other_ids["pmid"] = crossref_work["PMID"]
    if "PMCID" in crossref_work:
        other_ids["pmcid"] = crossref_work["PMCID"]

    # Publication status
    publication_status = "ACTIVE"
    if crossref_work.get("type") == "posted-content":
        publication_status = "PREPRINT"

    # Update type (retraction, correction, etc.)
    update_type = crossref_work.get("update-to", [])
    if update_type:
        for update in update_type:
            if update.get("type") == "retraction":
                publication_status = "RETRACTED"
            elif update.get("type") == "correction":
                publication_status = "CORRECTED"

    return {
        "title": title,
        "authors": authors,
        "journal": journal,
        "publication_date": publication_date,
        "publication_date_precision": publication_date_precision,
        "raw_date_parts": raw_date_parts,
        "doi": doi,
        "abstract": abstract,
        "other_ids": other_ids,
        "publication_status": publication_status,
        "raw": crossref_work,  # 保留原始数据以备后用
    }
