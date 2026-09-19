from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_mini_app_monitoring_controls_update_server_state() -> None:
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    bot_source = (ROOT / "src" / "legal_entity_agent" / "telegram_bot.py").read_text(encoding="utf-8")

    assert "send('unwatch', row.query)" in html
    assert "action === 'unwatch'" in html
    assert 'action not in {"check", "watch", "unwatch"}' in bot_source
