import pytest

from legal_entity_agent import telegram_bot


def test_telegram_proxy_is_optional(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_PROXY", raising=False)

    assert telegram_bot._telegram_proxy() is None


@pytest.mark.parametrize(
    "proxy",
    [
        "https://proxy.example:443",
        "http://user:password@proxy.example:8080",
        "socks5://user:password@proxy.example:1080",
        "socks5h://proxy.example:1080",
    ],
)
def test_telegram_proxy_accepts_supported_schemes(monkeypatch, proxy: str) -> None:
    monkeypatch.setenv("TELEGRAM_PROXY", proxy)

    assert telegram_bot._telegram_proxy() == proxy


@pytest.mark.parametrize(
    "proxy",
    ["", "proxy.example:1080", "ftp://proxy.example:21", "socks4://proxy.example:1080", "https://"],
)
def test_telegram_proxy_rejects_invalid_values(monkeypatch, proxy: str) -> None:
    monkeypatch.setenv("TELEGRAM_PROXY", proxy)

    if not proxy:
        assert telegram_bot._telegram_proxy() is None
    else:
        with pytest.raises(RuntimeError, match="TELEGRAM_PROXY"):
            telegram_bot._telegram_proxy()
