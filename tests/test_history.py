from datetime import datetime

from legal_entity_agent.history import HistoryStore
from legal_entity_agent.models import Assessment, FnsEntityRecord, Identifier, IdentifierKind, InaccuracyState, SearchQuery


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
