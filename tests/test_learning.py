import json
import sqlite3
from datetime import datetime

from legal_entity_agent.learning import FeedbackLabel, LearningStore
from legal_entity_agent.models import (
    Assessment,
    FnsEntityRecord,
    Identifier,
    IdentifierKind,
    InaccuracyState,
)


def _assessment() -> tuple[Identifier, Assessment]:
    identifier = Identifier("7707083893", IdentifierKind.INN)
    record = FnsEntityRecord(
        query=identifier,
        source_url="https://egrul.nalog.ru/search-result/test",
        fetched_at=datetime.now().astimezone(),
        name="ООО Ромашка",
        inn=identifier.value,
        inaccuracy_state=InaccuracyState.ABSENT,
    )
    return identifier, Assessment(record)


def test_feedback_requires_owner_and_export_requires_admin_review(tmp_path) -> None:
    identifier, assessment = _assessment()
    store = LearningStore(tmp_path / "learning.sqlite3", hash_salt="test-salt")
    event_id = store.record_check(
        chat_id=10,
        actor_id="100",
        identifier=identifier,
        assessment=assessment,
        rendered_response="ответ",
    )

    assert not store.add_feedback(
        event_id=event_id, chat_id=10, actor_id="different", label=FeedbackLabel.INCORRECT
    )
    assert not store.add_feedback(
        event_id=event_id, chat_id=11, actor_id="100", label=FeedbackLabel.INCORRECT
    )
    assert store.add_feedback(
        event_id=event_id,
        chat_id=10,
        actor_id="100",
        label=FeedbackLabel.NEEDS_REVIEW,
        note="Проверьте вручную",
    )
    assert not store.review(event_id=event_id, chat_id=11, reviewer_id="admin", approved=True)
    assert store.review(event_id=event_id, chat_id=10, reviewer_id="admin", approved=True, correction="OK")

    output = tmp_path / "learning.jsonl"
    assert store.export_jsonl(output, chat_id=10) == 1
    assert store.export_jsonl(tmp_path / "other-chat.jsonl", chat_id=11) == 0
    item = json.loads(output.read_text(encoding="utf-8"))
    assert item["query"] is None
    assert item["review_decision"] == "approved"
    assert item["correction"] == "OK"
    store.close()


def test_raw_mode_exports_query_and_rejected_is_excluded(tmp_path) -> None:
    identifier, assessment = _assessment()
    store = LearningStore(tmp_path / "learning.sqlite3", store_raw=True)
    event_id = store.record_check(
        chat_id=10, actor_id="100", identifier=identifier, assessment=assessment, rendered_response="ответ"
    )
    assert store.add_feedback(event_id=event_id, chat_id=10, actor_id="100", label=FeedbackLabel.CORRECT)
    assert store.review(event_id=event_id, chat_id=10, reviewer_id="admin", approved=False)
    output = tmp_path / "learning.jsonl"
    assert store.export_jsonl(output, chat_id=10) == 0
    store.close()


def test_legacy_learning_schema_gets_chat_scope_migration(tmp_path) -> None:
    database = tmp_path / "legacy-learning.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE learning_events (
                event_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                actor_hash TEXT NOT NULL,
                query_kind TEXT NOT NULL,
                query_hash TEXT NOT NULL,
                response_state TEXT NOT NULL,
                source_url TEXT NOT NULL,
                raw_query TEXT,
                rendered_response TEXT,
                feedback TEXT,
                feedback_note TEXT,
                feedback_at TEXT,
                reviewed INTEGER NOT NULL DEFAULT 0,
                reviewed_at TEXT,
                reviewer_hash TEXT,
                review_decision TEXT,
                correction TEXT
            )
            """
        )

    store = LearningStore(database, hash_salt="salt")
    columns = {row["name"] for row in store._connection.execute("PRAGMA table_info(learning_events)")}

    assert "chat_id" in columns
    assert store.pending(123) == []
    store.close()
