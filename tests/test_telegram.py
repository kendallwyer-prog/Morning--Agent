"""Telegram payload shaping and update parsing (no network involved)."""

from geofence.notify.telegram import (
    TelegramClient,
    parse_callback_data,
    parse_update,
)


def test_parses_a_button_tap():
    u = parse_update(
        {
            "update_id": 9,
            "callback_query": {
                "id": "abc",
                "data": "omw:42",
                "message": {"message_id": 7, "chat": {"id": 555}},
            },
        }
    )
    assert u.kind == "callback"
    assert u.data == "omw:42"
    assert u.chat_id == "555"
    assert u.callback_query_id == "abc"


def test_parses_a_text_message():
    u = parse_update(
        {"update_id": 3, "message": {"message_id": 1, "text": "/coffee",
                                     "chat": {"id": 555}}}
    )
    assert u.kind == "text"
    assert u.text == "/coffee"


def test_ignores_shapes_we_do_not_handle():
    # A photo with no caption, an edited message, a poll answer...
    assert parse_update({"update_id": 4, "message": {"photo": []}}) is None
    assert parse_update({"edited_message": {}}) is None


def test_callback_data_round_trip():
    assert parse_callback_data("skip:17") == ("skip", 17)
    assert parse_callback_data("garbage") == (None, None)
    assert parse_callback_data("omw:notanumber") == (None, None)
    assert parse_callback_data(None) == (None, None)


def test_send_shapes_the_inline_keyboard():
    calls = []
    client = TelegramClient("t", "555", _transport=lambda m, p: calls.append((m, p))
                            or {"message_id": 1})
    client.send("hello", [("A", "omw:1"), ("B", "skip:1")])
    method, payload = calls[0]
    assert method == "sendMessage"
    assert payload["chat_id"] == "555"
    assert payload["reply_markup"]["inline_keyboard"] == [
        [{"text": "A", "callback_data": "omw:1"},
         {"text": "B", "callback_data": "skip:1"}]
    ]


def test_network_failure_is_reported_not_raised():
    """A dropped connection must never take down the agent loop."""
    client = TelegramClient("t", "555", _transport=lambda m, p: None)
    result = client.send("hello")
    assert result.ok is False
    assert client.get_updates(None, 1) == []
