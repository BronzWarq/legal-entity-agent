from legal_entity_agent.permissions import (
    AccessPolicy,
    ChatAccessStore,
    is_main_admin,
    parse_tags,
)


def test_tags_are_case_insensitive_and_support_at_sign() -> None:
    policy = AccessPolicy(parse_tags("@Alice, bob"), parse_tags("@Admin"))
    assert policy.is_allowed("alice")
    assert policy.is_allowed("BOB")
    assert policy.is_admin("admin")
    assert not policy.is_allowed("unknown")


def test_main_admin_is_bound_to_numeric_user_id(monkeypatch) -> None:
    monkeypatch.setenv("MAIN_ADMIN_USER_ID", "1001")
    assert is_main_admin("@operator", user_id=1001)
    assert is_main_admin("@renamed_account", user_id="1001")
    assert not is_main_admin("@operator", user_id=1002)
    assert not is_main_admin("@operator")


def test_chat_access_is_separate_and_revoke_is_stable_by_user_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAIN_ADMIN_USER_ID", "1")
    store = ChatAccessStore(tmp_path / "access.sqlite3")
    assert store.is_allowed(1, user_id=1, username="operator")
    assert not store.grant(1, username="operator", user_id=1, granted_by=1)
    assert not store.revoke(1, username="operator", user_id=1, revoked_by=1)
    assert not store.is_allowed(1, user_id=2, username="Alice")
    assert store.grant(1, username="Alice", user_id=2, granted_by=1)
    assert store.is_allowed(1, user_id=2, username="Alice")
    assert not store.is_allowed(2, user_id=2, username="Alice")
    assert store.is_allowed(1, user_id=2, username="alice_new")
    assert store.revoke(1, username="Alice", revoked_by=1)
    assert not store.is_allowed(1, user_id=2, username="alice_new")
    assert len(store.list_trusted(1)) == 0
    store.close()


def test_revoke_by_reply_user_id_survives_username_change(tmp_path) -> None:
    store = ChatAccessStore(tmp_path / "access.sqlite3")
    assert store.grant(1, username="old_name", user_id=2, granted_by=1)
    assert store.revoke(1, username="new_name", user_id=2, revoked_by=1)
    assert not store.is_allowed(1, user_id=2, username="new_name")
    store.close()
