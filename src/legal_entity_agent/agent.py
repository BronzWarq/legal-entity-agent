from __future__ import annotations

from .fns_client import FnsEgrulClient
from .identifiers import parse_search_query
from .models import Assessment, SearchQuery


class LegalEntityAgent:
    """Оркестратор проверки юридического лица."""

    def __init__(self, client: FnsEgrulClient | None = None) -> None:
        self.client = client or FnsEgrulClient()

    async def check(self, value: str | SearchQuery) -> Assessment:
        query = value if isinstance(value, SearchQuery) else parse_search_query(value)
        record = await self.client.lookup(query)
        return Assessment(record)
