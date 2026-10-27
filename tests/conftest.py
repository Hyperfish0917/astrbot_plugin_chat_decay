"""Minimal AstrBot 4.29.3 test doubles: these are NOT real integration tests."""

import importlib
import sys
import types

import pytest


class FakeEvent:
    def __init__(self, text="普通消息", group="1", adapter="adapterA", bot="9", sender="2", native=False, parts=None):
        self.message_str = text
        self.unified_msg_origin = f"{adapter}:group:{group}"
        self._self = bot
        self._sender = sender
        self.is_at_or_wake_command = native
        self.parts = parts or []
        self.stop_calls = 0

    def get_self_id(self):
        return self._self

    def get_sender_id(self):
        return self._sender

    def get_messages(self):
        return self.parts

    def stop_event(self):
        self.stop_calls += 1


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saves = 0

    def save_config(self):
        self.saves += 1


class FakeContext:
    def __init__(self):
        self.routes = {}

    def register_web_api(self, route, handler, methods, desc):
        self.routes[route] = (handler, methods, desc)


@pytest.fixture
def module(monkeypatch):
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")

    class Filter:
        class EventMessageType:
            GROUP_MESSAGE = "group"

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda method: method

    class Star:
        def __init__(self, context):
            self.context = context

    event.filter = Filter
    event.AstrMessageEvent = FakeEvent
    star.Star = Star
    star.register = lambda *args, **kwargs: (lambda cls: cls)
    web.json_response = lambda payload: payload
    for name, obj in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.api.web": web,
    }.items():
        monkeypatch.setitem(sys.modules, name, obj)

    # Plugins installed at repository root are imported as main by AstrBot.
    sys.modules.pop("main", None)
    mod = importlib.import_module("main")
    yield mod
    sys.modules.pop("main", None)


@pytest.fixture
def plugin(module):
    context = FakeContext()
    return module.ChatDecayPlugin(context, FakeConfig())


@pytest.fixture
def roll(monkeypatch, module):
    state = {"now": 100.0, "random": 0.01}
    monkeypatch.setattr(module.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(module.random, "random", lambda: state["random"])
    return state
