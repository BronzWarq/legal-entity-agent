from datetime import datetime

from legal_entity_agent.history import HistoryStore
from legal_entity_agent.models import (
    Assessment,
    FnsEntityRecord,
    Identifier,
    IdentifierKind,
    InaccuracyState,
    SearchQuery,
)


def _assessment() -> Assessment:
    identifier = Identifier("7707083893", IdentifierKind.INN)
    return Assessment(
        FnsEntityRecord(
            query=SearchQuery("ИНН 7707083893 КПП 770401001", (identifier,)),
            source_url="https://egrul.nalog.ru/search-result/test",
            fetched_at=datetime.now().astimezone(),
            name="ООО Ромашка",
            inn="7707083893",
            kpp="770401001",
            inaccuracy_state=InaccuracyState.ABSENT,
        )
    )


def test_history_is_scoped_to_chat_and_user(tmp_path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3", hash_salt="salt")
    query = SearchQuery("ИНН 7707083893 КПП 770401001")
    assessment = _assessment()
    store.record_check(
        event_id="one",
        chat_id=10,
        actor_id=100,
        query=query,
        assessment=assessment,
        report="Итог проверки 1",
    )
    store.record_check(
        event_id="two",
        chat_id=10,
        actor_id=200,
        query=query,
        assessment=assessment,
        report="Итог проверки 2",
    )
    store.record_check(
        event_id="three",
        chat_id=20,
        actor_id=100,
        query=query,
        assessment=assessment,
        report="Другой чат",
    )

    assert [entry.event_id for entry in store.list_for(chat_id=10, actor_id=100, is_root=False)] == ["one"]
    assert {entry.event_id for entry in store.list_for(chat_id=10, actor_id=999, is_root=True)} == {"one", "two"}
    assert store.get_for(event_id="two", chat_id=10, actor_id=100, is_root=False) is None
    assert store.get_for(event_id="two", chat_id=10, actor_id=999, is_root=True).report == "Итог проверки 2"
    store.close()


def test_list_unique_queries_is_scoped_to_current_chat(tmp_path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3", hash_salt="salt")
    assessment = _assessment()
    store.record_check(
        event_id="by-inn",
        chat_id=10,
        actor_id=100,
        query=SearchQuery("ИНН 7707083893"),
        assessment=assessment,
        report="По ИНН",
    )
    store.record_check(
        event_id="by-name",
        chat_id=20,
        actor_id=200,
        query=SearchQuery("ООО Ромашка, Москва"),
        assessment=assessment,
        report="По названию",
    )
    store.record_check(
        event_id="free-text",
        chat_id=30,
        actor_id=300,
        query=SearchQuery("новая компания без реквизитов"),
        assessment=Assessment(
            FnsEntityRecord(
                query=SearchQuery("новая компания без реквизитов"),
                source_url="https://egrul.nalog.ru/search-result/free",
                fetched_at=datetime.now().astimezone(),
            )
        ),
        report="Свободный поиск",
    )
    store.record_check(
        event_id="free-text-current-chat",
        chat_id=10,
        actor_id=100,
        query=SearchQuery("свободный запрос текущего чата"),
        assessment=Assessment(
            FnsEntityRecord(
                query=SearchQuery("свободный запрос текущего чата"),
                source_url="https://egrul.nalog.ru/search-result/current",
                fetched_at=datetime.now().astimezone(),
            )
        ),
        report="Свободный поиск текущего чата",
    )

    queries = store.list_unique_queries(chat_id=10, actor_id=999, is_root=True)

    assert {query.value for query in queries} == {"ИНН 7707083893", "свободный запрос текущего чата"}
    assert store.list_unique_queries(chat_id=20, actor_id=999, is_root=True)[0].value == "ООО Ромашка, Москва"
    assert store.list_unique_queries(chat_id=10, actor_id=999, is_root=False) == []
    store.close()
