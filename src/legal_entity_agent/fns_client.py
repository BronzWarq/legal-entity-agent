from __future__ import annotations

import json
from typing import Any

import httpx

from .identifiers import parse_search_query
from .models import FnsEntityRecord, Identifier, SearchQuery
from .parser import parse_fns_payload


class FnsError(RuntimeError):
    """Ошибка получения или разбора ответа сервиса ФНС."""


class FnsBlockedError(FnsError):
    """Сервис запросил CAPTCHA или заблокировал автоматический запрос."""


class FnsNotFoundError(FnsError):
    """Поисковый запрос не вернул юридическое лицо."""


class FnsEgrulClient:
    """Клиент публичного веб-сервиса ЕГРЮЛ/ЕГРИП ФНС.

    Сервис ФНС не заявляет этот веб-интерфейс как стабильный API. Поэтому
    endpoint вынесен в настройки, ответы проверяются консервативно, а CAPTCHA
    не обходится: в таком случае клиент возвращает FnsBlockedError.
    """

    def __init__(
        self,
        base_url: str = "https://egrul.nalog.ru/",
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.transport = transport

    async def lookup(self, value: str | Identifier | SearchQuery) -> FnsEntityRecord:
        if isinstance(value, SearchQuery):
            query = value
        elif isinstance(value, Identifier):
            query = SearchQuery(value.value, (value,))
        else:
            query = parse_search_query(value)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "legal-entity-agent/0.1 (+https://egrul.nalog.ru/)",
        }
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            try:
                search = await client.post("", data={"query": query.value})
                search.raise_for_status()
            except httpx.HTTPError as exc:
                raise FnsError(f"Не удалось выполнить запрос к ФНС: {exc}") from exc

            search_payload = self._json_or_blocked(search)
            token = search_payload.get("t") or search_payload.get("token")
            if not token:
                if self._contains_captcha(search.text):
                    raise FnsBlockedError("ФНС запросила CAPTCHA или ограничила автоматический запрос.")
                raise FnsError("Формат ответа ФНС не содержит токен результата поиска.")

            try:
                result = await client.get(f"search-result/{token}")
                result.raise_for_status()
            except httpx.HTTPError as exc:
                raise FnsError(f"Не удалось получить результат поиска ФНС: {exc}") from exc

            payload = self._json_or_blocked(result)
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if rows == [] or (not rows and not self._looks_like_entity(payload)):
                raise FnsNotFoundError(f"По запросу {query.value} сведения не найдены.")
            return parse_fns_payload(payload, query, str(result.url))

    @staticmethod
    def _contains_captcha(text: str) -> bool:
        lowered = text.casefold()
        return "captcha" in lowered or "капч" in lowered or "проверить, что вы не робот" in lowered

    def _json_or_blocked(self, response: httpx.Response) -> dict[str, Any]:
        if self._contains_captcha(response.text):
            raise FnsBlockedError("ФНС запросила CAPTCHA или ограничила автоматический запрос.")
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise FnsError("ФНС вернула ответ, который не удалось разобрать как JSON.") from exc
        if not isinstance(payload, dict):
            raise FnsError("ФНС вернула неожиданный формат ответа.")
        return payload

    @staticmethod
    def _looks_like_entity(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        text = json.dumps(payload, ensure_ascii=False).casefold()
        return any(
            marker in text
            for marker in ("инн", "inn", "огрн", "ogrn", "кпп", "kpp", "наименование", "название", "name", "fullname")
        )
