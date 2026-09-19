from legal_entity_agent.batch_review import BatchReviewStore


def test_batch_review_store_keeps_pending_and_resolves(tmp_path) -> None:
    store = BatchReviewStore(tmp_path / "review.sqlite3", hash_salt="salt")
    store.record_pending(
        review_id="job:0",
        chat_id=1,
        actor_id=10,
        query_text="ИНН 7707083893",
        reason="требуется ручная проверка CAPTCHA",
        source="history",
    )

    entries = store.list_pending(chat_id=1, actor_id=10, is_root=False)
    assert len(entries) == 1
    assert entries[0].review_id == "job:0"
    assert store.resolve("job:0") is True
    assert store.list_pending(chat_id=1, actor_id=10, is_root=False) == []
    store.close()
