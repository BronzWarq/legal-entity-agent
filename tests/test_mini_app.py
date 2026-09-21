from pathlib import Path

from legal_entity_agent.telegram_bot import _mini_app_reply_keyboard

ROOT = Path(__file__).parents[1]


def test_mini_app_monitoring_controls_update_server_state() -> None:
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    bot_source = (ROOT / "src" / "legal_entity_agent" / "telegram_bot.py").read_text(encoding="utf-8")

    assert "send('unwatch', row.query)" in html
    assert "action === 'unwatch'" in html
    assert "localStorage" in html
    assert "initDataUnsafe?.user?.id" in html
    assert "initDataUnsafe?.chat?.id" in html
    assert "params.get('chat_id')" in html
    assert "const storageKey" in html
    assert "const persistRows" in html
    assert "const rememberQuery" in html
    assert 'id="shareList"' in html
    assert "action === 'share_list'" in html
    assert "shared_queries" in html
    assert "Добавлено компаний из общего списка" in html
    assert 'id="exportExcel"' in html
    assert "send('export_excel')" in html
    assert "формате .xlsx" in html
    assert 'action == "export_excel"' in bot_source
    assert 'action not in {"check", "watch", "unwatch"}' in bot_source
    assert 'class="hero-chip"' in html
    assert "prefers-reduced-motion" in html


def test_mini_app_link_carries_chat_context() -> None:
    bot_source = (ROOT / "src" / "legal_entity_agent" / "telegram_bot.py").read_text(encoding="utf-8")

    assert "chat_id: int | str | None = None" in bot_source
    assert "_mini_app_keyboard(chat_id=message.chat.id)" in bot_source
    assert "chat_id={quote(str(chat_id), safe='')}" in bot_source


def test_private_chat_uses_reply_web_app_button_for_send_data(monkeypatch) -> None:
    monkeypatch.setenv("MINI_APP_URL", "https://example.test/mini_app/")

    keyboard = _mini_app_reply_keyboard(chat_id=12345)

    assert keyboard is not None
    assert keyboard.one_time_keyboard is True
    assert keyboard.keyboard[0][0].text == "Открыть Mini App"
    assert keyboard.keyboard[0][0].web_app.url == "https://example.test/mini_app/?chat_id=12345&transport=web_app_data"
