from legal_entity_agent.watchlist import WatchStore


def test_watchlist_notifies_only_after_snapshot_changes() -> None:
    store = WatchStore(":memory:")
    store.add(1, "ИНН 7707083893", 42)
    assert store.update_snapshot(1, "ИНН 7707083893", "first") is False
    assert store.update_snapshot(1, "ИНН 7707083893", "first") is False
    assert store.update_snapshot(1, "ИНН 7707083893", "second") is True


def test_watchlist_remembers_inaccuracy_state() -> None:
    store = WatchStore(":memory:")
    store.add(1, "ИНН 7707083893", 42)
    assert store.snapshot_state(1, "ИНН 7707083893") == (None, None)
    store.update_snapshot_with_state(1, "ИНН 7707083893", "first", inaccuracy_state="absent")
    digest, state = store.snapshot_state(1, "ИНН 7707083893")
    assert digest is not None
    assert state == "absent"
