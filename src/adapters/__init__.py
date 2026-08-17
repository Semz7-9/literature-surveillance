"""外部数据源适配器"""

from .crossref import CrossrefAdapter, parse_crossref_metadata

__all__ = ["CrossrefAdapter", "parse_crossref_metadata"]
