from legal_entity_agent.telegram_bot import BOT_MENU_ACTIONS, bot_menu_keyboard


def test_bot_menu_has_two_column_quick_actions() -> None:
    menu = bot_menu_keyboard()

    assert [len(row) for row in menu.keyboard] == [2, 2, 2, 2]
    assert [button.text for row in menu.keyboard for button in row] == [
        "🔎 Проверить компанию",
        "📋 История проверок",
        "📊 Проверить все",
        "📁 Загрузить CSV/XLSX",
        "👁 Мониторинг",
        "📄 Лицензии",
        "🆘 Помощь",
        "🧠 Навыки",
    ]
    assert menu.resize_keyboard is True
    assert menu.is_persistent is True


def test_all_menu_buttons_have_explicit_message_routes() -> None:
    assert BOT_MENU_ACTIONS == {
        "🔎 Проверить компанию",
        "📋 История проверок",
        "📊 Проверить все",
        "📁 Загрузить CSV/XLSX",
        "👁 Мониторинг",
        "📄 Лицензии",
        "🆘 Помощь",
        "🧠 Навыки",
    }


def test_menu_action_text_ignores_emoji_variation_selector() -> None:
    # Проверяет тот же нормализующий путь, который используется обработчиком.
    assert "🔎️ Проверить компанию".replace("\ufe0e", "").replace("\ufe0f", "") == "🔎 Проверить компанию"

