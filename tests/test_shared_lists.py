from datetime import UTC, datetime, timedelta

import pytest

from legal_entity_agent.shared_lists import SharedListStore


def test_shared_list_is_available_only_in_original_chat() -> None:
    store = SharedListStore(":memory:")
    shared = store.create(10, 20, ["7707083893", "7707083893", "ООО Ромашка"])

    assert shared.queries == ("7707083893", "ООО Ромашка")
    assert store.get(shared.share_id, 10) == shared
    assert store.get(shared.share_id, 11) is None
    store.close()


def test_shared_list_rejects_empty_and_too_large_lists() -> None:
    store = SharedListStore(":memory:")

    with pytest.raises(ValueError):
        store.create(10, 20, [" "])
    with pytest.raises(ValueError):
        store.create(10, 20, [str(item) for item in range(101)])
    store.close()


def test_expired_shared_list_is_removed() -> None:
    store = SharedListStore(":memory:")
    shared = store.create(10, 20, ["7707083893"])
    with store._db:
        store._db.execute(
            "UPDATE shared_lists SET expires_at=? WHERE share_id=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), shared.share_id),
        )

    assert store.get(shared.share_id, 10) is None
    store.close()

