from __future__ import annotations

import json
from typing import Any

import httpx

from .fns_client import FnsError, FnsNotFoundError, FnsTransientError
from .identifiers import parse_search_query
from .models import FnsEntityRecord, Identifier, SearchQuery
from .parser import parse_fns_payload

DEFAULT_DADATA_FIND_BY_ID_URL = (
    "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
)
DEFAULT_DADATA_SUGGEST_URL = (
    "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
)


class DadataError(FnsError):
    """Ошибка API DaData, не связанная с временной недоступностью."""


class DadataClient:
    """Клиент API DaData с нормализацией в модель проверки бота.

    API-ключ используется только на сервере и никогда не попадает в source_url
    или сохранённый ответ.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_DADATA_FIND_BY_ID_URL,
        suggest_url: str = DEFAULT_DADATA_SUGGEST_URL,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("Для DadataClient нужен непустой API-ключ.")
        self.base_url = base_url.rstrip("/")
        self.suggest_url = suggest_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def lookup(self, value: str | Identifier | SearchQuery) -> FnsEntityRecord:
        if isinstance(value, SearchQuery):
            query = value
        elif isinstance(value, Identifier):
            query = SearchQuery(value.value, (value,))
        else:
            query = parse_search_query(value)

        url = self.base_url if query.identifiers else self.suggest_url
        headers = {
            "Accept": "application/json",
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                headers=headers,
                timeout=self.timeout,
                follow_redirects=True,
                transport=self.transport,
            ) as client:
                response = await client.post(url, json={"query": query.value})
        except httpx.RequestError as exc:
            raise FnsTransientError(f"Не удалось выполнить запрос к DaData: {exc}") from exc

        if response.status_code in {401, 403}:
            raise DadataError("DaData отклонила API-ключ. Проверьте токен и тариф.")
        if response.status_code == 429 or response.status_code >= 500:
            raise FnsTransientError(
                f"DaData временно недоступна: HTTP {response.status_code}."
            )
        if response.is_error:
            raise DadataError(f"DaData вернула ошибку HTTP {response.status_code}.")

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise DadataError("DaData вернула ответ, который не удалось разобрать как JSON.") from exc
        if not isinstance(payload, dict):
            raise DadataError("DaData вернула неожиданный формат ответа.")

        suggestions = payload.get("suggestions")
        if not isinstance(suggestions, list) or not suggestions:
            raise FnsNotFoundError(f"По запросу {query.value} сведения не найдены.")

        suggestion = suggestions[0]
        if not isinstance(suggestion, dict):
            raise DadataError("DaData вернула некорректную запись компании.")
        data = suggestion.get("data")
        if not isinstance(data, dict):
            raise DadataError("DaData вернула запись без блока data.")

        normalized = self._normalize_suggestion(suggestion, data)
        record = parse_fns_payload(normalized, query, str(response.url))
        record.raw = {
            "provider": "dadata",
            "response": payload,
        }
        return record

    @staticmethod
    def _normalize_suggestion(
        suggestion: dict[str, Any],
        data: dict[str, Any],
    ) -> dict[str, Any]:
        name_data = data.get("name")
        if isinstance(name_data, dict):
            name = (
                name_data.get("full_with_opf")
                or name_data.get("short_with_opf")
                or name_data.get("full")
            )
        else:
            name = name_data
        name = name or suggestion.get("value") or suggestion.get("unrestricted_value")

        state = data.get("state")
        state = state if isinstance(state, dict) else {}
        address = data.get("address")
        address = address if isinstance(address, dict) else {}
        normalized: dict[str, Any] = {
            "name": name,
            "inn": data.get("inn"),
            "ogrn": data.get("ogrn"),
            "kpp": data.get("kpp"),
            "status": state.get("status"),
            "registrationDate": state.get("registration_date"),
            "address": address.get("value"),
        }

        if "invalid" in data:
            # Для DaData null означает, что недостоверные сведения не выявлены.
            normalized["inaccuracy"] = data.get("invalid") is True
            normalized["inaccuracy_markers"] = DadataClient._inaccuracy_markers(data)
        return normalized

    @staticmethod
    def _inaccuracy_markers(data: dict[str, Any]) -> list[str]:
        markers: list[str] = []
        for field, label in (
            ("founders", "учредителе"),
            ("managers", "руководителе"),
        ):
            items = data.get(field)
            if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("invalidity")
                for item in items
            ):
                markers.append(f"Недостоверные сведения об {label}.")
        address = data.get("address")
        if isinstance(address, dict) and address.get("invalidity"):
            markers.append("Недостоверные сведения об адресе.")
        if data.get("invalid") is True and not markers:
            markers.append("Внесена запись о недостоверности сведений.")
        return markers
