"""Two-way Telegram control: command parsing, authorisation, acks, backlog-skip, fail-safe."""

from __future__ import annotations

from algo_trading.observability.telegram_commands import (
    TelegramCommandListener,
    parse_command,
)


class FakeHTTP:
    def __init__(self, prime: list[dict] | None = None) -> None:
        self._prime = prime or []
        self.sent: list[str] = []
        self.fail_send = False

    def get_updates(self, offset, timeout):
        return self._prime  # only used by _prime_offset in these tests

    def send(self, text):
        if self.fail_send:
            raise RuntimeError("boom")
        self.sent.append(text)


def _listener(http=None):
    enqueued: list[str] = []
    lis = TelegramCommandListener("TOK", "999", enqueued.append, http=http or FakeHTTP())
    return lis, enqueued


def _msg(chat_id, text, update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


# -- parsing ---------------------------------------------------------------------------

def test_parse_command_mappings():
    assert parse_command("/clear") == "flatten"
    assert parse_command("/stop") == "stop"
    assert parse_command("/STOP") == "stop"  # case-insensitive
    assert parse_command("/stop@cjigar_bot") == "stop"  # strips @botname
    assert parse_command("/clear now please") == "flatten"  # only first token matters
    assert parse_command("/help") == "help"
    assert parse_command("/start") == "help"
    assert parse_command("hello") is None
    assert parse_command("") is None


# -- authorisation + dispatch ----------------------------------------------------------

def test_authorised_clear_enqueues_flatten_and_acks():
    http = FakeHTTP()
    lis, enq = _listener(http)
    lis._handle(_msg("999", "/clear"))
    assert enq == ["flatten"]
    assert http.sent and "Flattening" in http.sent[0]


def test_authorised_stop_enqueues_stop():
    http = FakeHTTP()
    lis, enq = _listener(http)
    lis._handle(_msg("999", "/stop"))
    assert enq == ["stop"]
    assert "Stopping" in http.sent[0]


def test_unauthorised_chat_is_ignored():
    http = FakeHTTP()
    lis, enq = _listener(http)
    lis._handle(_msg("12345", "/stop"))  # not the operator chat
    assert enq == []  # never acts for anyone else
    assert http.sent == []


def test_unknown_text_does_nothing():
    http = FakeHTTP()
    lis, enq = _listener(http)
    lis._handle(_msg("999", "good morning"))
    assert enq == [] and http.sent == []


def test_help_replies_without_enqueue():
    http = FakeHTTP()
    lis, enq = _listener(http)
    lis._handle(_msg("999", "/help"))
    assert enq == []
    assert "/clear" in http.sent[0] and "/stop" in http.sent[0]


# -- backlog-skip + fail-safe ----------------------------------------------------------

def test_prime_offset_skips_backlog():
    http = FakeHTTP(prime=[{"update_id": 41}, {"update_id": 42}])
    lis, _ = _listener(http)
    lis._prime_offset()
    assert lis._offset == 43  # next poll starts past the backlog -> stale commands not replayed


def test_reply_failure_is_fail_safe():
    http = FakeHTTP()
    http.fail_send = True
    lis, enq = _listener(http)
    # a broken sendMessage must not raise; the command still enqueued
    lis._handle(_msg("999", "/clear"))
    assert enq == ["flatten"]
