import datetime as dt
import json

import pytest

from personal_site.ai import AiConfig, handle_ntfy_message
from personal_site.ai_models import AiMessage
from personal_site.caffeine_models import CaffeineEntry
from personal_site.db import Base, create_engine_and_sessionmaker
from personal_site.notify import NtfyConfig
from personal_site.sleep_models import SleepEntry


class _FakeToolCall:
    def __init__(self, name: str, arguments: dict):
        self.type = "function_call"
        self.name = name
        self.arguments = json.dumps(arguments)


class _FakeResponse:
    def __init__(self, tool_call: _FakeToolCall):
        self.output = [tool_call]


class _FakeOpenAI:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.responses = self

    def create(self, **kwargs):
        return self._response


@pytest.fixture()
def session():
    engine, SessionLocal = create_engine_and_sessionmaker("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        yield session


def _make_ai_cfg():
    return AiConfig(enabled=True, model="gpt-test", api_key="test-key", debug_log=False)


def _make_ntfy_cfg():
    return NtfyConfig(base_url="https://ntfy.invalid", topic="test")


def test_sleep_message_logs_entry(monkeypatch, session):
    fake_client = _FakeOpenAI()
    fake_client._response = _FakeResponse(
        _FakeToolCall(
            "log_sleep",
            {
                "slept_on": None,
                "duration_hours": 7,
                "duration_minutes": 30,
                "quality": 2,
                "notes": None,
            },
        )
    )

    monkeypatch.setattr("personal_site.ai.OpenAI", lambda api_key=None: fake_client)
    monkeypatch.setattr("personal_site.ai.send_ntfy", lambda **kwargs: "msg-id")

    result = handle_ntfy_message(
        session=session,
        ntfy_cfg=_make_ntfy_cfg(),
        ai_cfg=_make_ai_cfg(),
        topic="test-topic",
        text="I slept for 7 hours and 30 minutes. I say the quality was about a 2.",
        received_event={"id": "evt-1"},
    )

    assert result["handled"] is True
    entry = session.query(SleepEntry).one()
    assert entry.duration_minutes == 450
    assert entry.quality == 2
    assert entry.slept_on == dt.date.today()

    assistant_msgs = (
        session.query(AiMessage).filter(AiMessage.role == "assistant").all()
    )
    assert assistant_msgs


def test_caffeine_message_logs_entry(monkeypatch, session):
    fake_client = _FakeOpenAI()
    fake_client._response = _FakeResponse(
        _FakeToolCall(
            "log_caffeine",
            {
                "consumed_on": None,
                "time_bucket": None,
                "amount_mg": 95,
                "source": "coffee",
                "notes": None,
            },
        )
    )

    monkeypatch.setattr("personal_site.ai.OpenAI", lambda api_key=None: fake_client)
    monkeypatch.setattr("personal_site.ai.send_ntfy", lambda **kwargs: "msg-id")

    result = handle_ntfy_message(
        session=session,
        ntfy_cfg=_make_ntfy_cfg(),
        ai_cfg=_make_ai_cfg(),
        topic="test-topic",
        text="Coffee, 95mg this morning.",
        received_event={"id": "evt-2"},
    )

    assert result["handled"] is True
    entry = session.query(CaffeineEntry).one()
    assert entry.amount_mg == 95
    assert entry.consumed_on == dt.date.today()
    assert entry.time_bucket in {"morning", "afternoon", "night"}

    assistant_msgs = (
        session.query(AiMessage).filter(AiMessage.role == "assistant").all()
    )
    assert assistant_msgs
