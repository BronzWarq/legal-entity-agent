import pytest

from legal_entity_agent.mini_app_security import (
    MiniAppContextStore,
    MiniAppRateLimiter,
    MiniAppSecurityError,
)


def test_launch_context_binds_chat_and_rejects_replayed_request() -> None:
    store = MiniAppContextStore()
    token = store.issue(-100123, chat_type="supergroup", ttl_seconds=60, now=1000)

    context = store.resolve(token, now=1059)
    assert context.chat_id == -100123
    assert context.chat_type == "supergroup"
    assert store.claim_request(token, "request-123456789", now=1059)
    assert not store.claim_request(token, "request-123456789", now=1059)

    with pytest.raises(MiniAppSecurityError):
        store.resolve(token, now=1060)
    assert not store.claim_request(token, "request-987654321", now=1060)


def test_context_store_can_revoke_all_links() -> None:
    store = MiniAppContextStore()
    token = store.issue(123, now=1000)
    store.revoke_all()

    with pytest.raises(MiniAppSecurityError):
        store.resolve(token, now=1000)


def test_rate_limiter_uses_sliding_window() -> None:
    limiter = MiniAppRateLimiter(limit=2, window_seconds=60)

    assert limiter.allow("user:1", now=1000)
    assert limiter.allow("user:1", now=1001)
    assert not limiter.allow("user:1", now=1002)
    assert limiter.allow("user:1", now=1061)
