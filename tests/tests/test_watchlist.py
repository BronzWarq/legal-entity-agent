from legal_entity_agent.watchlist import WatchStore


def test_watchlist_notifies_only_after_snapshot_changes() -> None:
    store = WatchStore(":memory:")
    store.add(1, "ИНН 7707083893", 42)
    assert store.update_snapshot(1, "ИНН 7707083893", "first") is False
    assert store.update_snapshot(1, "ИНН 7707083893", "first") is False
    assert store.update_snapshot(1, "ИНН 7707083893", "second") is True
