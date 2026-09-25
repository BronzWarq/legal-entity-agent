import json
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
