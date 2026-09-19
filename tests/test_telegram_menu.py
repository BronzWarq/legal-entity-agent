from legal_entity_agent.telegram_bot import bot_menu_keyboard


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
