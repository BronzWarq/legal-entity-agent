from legal_entity_agent.permissions import AccessPolicy, ChatAccessStore, is_main_admin, parse_tags


def test_tags_are_case_insensitive_and_support_at_sign() -> None:
    policy = AccessPolicy(parse_tags("@Alice, bob"), parse_tags("@Admin"))
    assert policy.is_allowed("alice")
    assert policy.is_allowed("BOB")
    assert policy.is_admin("admin")
    assert not policy.is_allowed("unknown")


def test_only_sholomon_is_main_admin() -> None:
    assert is_main_admin("@Sholomon")
    assert is_main_admin("sholomon")
    assert not is_main_admin("@admin")


def test_chat_access_is_separate_and_revoke_is_stable_by_user_id(tmp_path) -> None:
    store = ChatAccessStore(tmp_path / "access.sqlite3")
    assert store.is_allowed(1, user_id=1, username="Sholomon")
    assert not store.is_allowed(1, user_id=2, username="Alice")
    assert store.grant(1, username="Alice", user_id=2, granted_by=1)
    assert store.is_allowed(1, user_id=2, username="Alice")
    assert not store.is_allowed(2, user_id=2, username="Alice")
    assert store.is_allowed(1, user_id=2, username="alice_new")
    assert store.revoke(1, username="Alice", revoked_by=1)
    assert not store.is_allowed(1, user_id=2, username="alice_new")
    assert len(store.list_trusted(1)) == 0
    store.close()
