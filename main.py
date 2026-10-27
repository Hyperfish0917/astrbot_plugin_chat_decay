"""AstrBot group-chat wake-up and probabilistic conversation decay.

Compatible with AstrBot v4.29.3's WakingCheckStage/ProcessStage semantics.
A passing ordinary message must set ``is_at_or_wake_command``; merely
returning from an event listener does NOT invoke the default LLM.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register
from astrbot.api.web import json_response

PLUGIN_NAME = "astrbot_plugin_chat_decay"
DEFAULTS = {
    "keywords": ["肥鱼", "鲸鱼", "deepseek"],
    "ignore_case": True,
    "base_passive_rate": 0.2,
    "decay_rate_1": 1.0,
    "decay_rate_2": 0.8,
    "decay_rate_3": 0.5,
    "max_turns": 3,
    "cooldown_seconds": 90,
    "session_timeout_seconds": 30,
}
RATE_KEYS = ("base_passive_rate", "decay_rate_1", "decay_rate_2", "decay_rate_3")


@dataclass
class GroupState:
    """In-memory per-adapter, per-bot, per-group state (not persisted)."""

    accepted_turns: int = 0
    last_accepted_at: float | None = None
    cooldown_until: float = 0.0


@register("chat_decay", "Hyperfish0917", "关键词唤醒与群聊概率衰减", "1.3.0")
class ChatDecayPlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.group_states: dict[str, GroupState] = {}
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings/reset-rates",
            self.reset_rates,
            ["POST"],
            "Reset probability settings",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings/reset-all",
            self.reset_all,
            ["POST"],
            "Reset all plugin settings",
        )

    def _get_cfg(self, key):
        value = self.config.get(key, DEFAULTS[key])
        return DEFAULTS[key] if value is None else value

    def _rate(self, key):
        try:
            rate = float(self._get_cfg(key))
            return rate if math.isfinite(rate) and 0 <= rate <= 1 else DEFAULTS[key]
        except (ValueError, TypeError, OverflowError):
            return DEFAULTS[key]

    def _positive_int(self, key, minimum, maximum):
        try:
            value = int(self._get_cfg(key))
            return min(max(value, minimum), maximum)
        except (ValueError, TypeError, OverflowError):
            return DEFAULTS[key]

    def _keyword_hit(self, message):
        words = self._get_cfg("keywords")
        if not isinstance(words, (list, tuple)) or not message:
            return False
        insensitive = bool(self._get_cfg("ignore_case"))
        haystack = message.casefold() if insensitive else message
        return any(
            (word.casefold() if insensitive else word) in haystack
            for word in words
            if isinstance(word, str) and word.strip()
        )

    @staticmethod
    def _direct_poke(event):
        """OneBot Poke component targeting this bot, not a generic group poke."""
        bot_id = str(event.get_self_id())
        if not bot_id:
            return False
        for part in event.get_messages():
            if part.__class__.__name__ != "Poke":
                continue
            target = getattr(part, "target_id", None)
            if callable(target):
                target = target()
            if target is None:
                target = getattr(part, "id", None) or getattr(part, "qq", None)
            if target is not None and str(target) == bot_id:
                return True
        return False

    def _group_key(self, event):
        # UMO includes the adapter instance, avoiding cross-adapter interference.
        return f"{event.unified_msg_origin}:{event.get_self_id()}"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=50)
    async def handle_group_message(self, event: AstrMessageEvent):
        # The WakingCheckStage has already recognized native @, reply-to-bot,
        # wake prefix, and commands. Never suppress those or other handlers.
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        message = (event.message_str or "").strip()
        if event.is_at_or_wake_command or self._keyword_hit(message) or self._direct_poke(event):
            now = time.monotonic()
            state = self.group_states.setdefault(self._group_key(event), GroupState())
            state.accepted_turns = 0
            state.last_accepted_at = now
            state.cooldown_until = 0.0
            event.is_at_or_wake_command = True
            return

        # Do not activate the default LLM on bot commands or empty messages.
        # This does not stop propagation to other plugins.
        if not message or message.startswith("/"):
            return

        now = time.monotonic()
        key = self._group_key(event)
        state = self.group_states.setdefault(key, GroupState())
        if now < state.cooldown_until:
            return
        timeout = self._positive_int("session_timeout_seconds", 1, 3600)
        if state.last_accepted_at is None or now - state.last_accepted_at > timeout:
            state.accepted_turns = 0
            state.last_accepted_at = None
        rates = (self._rate("decay_rate_1"), self._rate("decay_rate_2"), self._rate("decay_rate_3"))
        probability = (
            self._rate("base_passive_rate")
            if state.last_accepted_at is None
            else rates[min(state.accepted_turns, 2)]
        )
        if random.random() >= probability:
            # A missed roll is not a turn and does not start cooldown.
            return

        # In AstrBot 4.29.3 ProcessStage, only this flag activates default LLM.
        event.is_at_or_wake_command = True
        if state.last_accepted_at is None:
            # Cold start is the conversation's first response; next roll uses decay_rate_1.
            state.accepted_turns = 0
        else:
            state.accepted_turns += 1
        state.last_accepted_at = now
        maximum = self._positive_int("max_turns", 1, 3)
        if state.accepted_turns >= maximum:
            state.cooldown_until = now + self._positive_int("cooldown_seconds", 0, 3600)
            state.accepted_turns = 0
            # last_accepted_at remains for informational state only; when
            # cooldown elapses, next roll returns to baseline.
            state.last_accepted_at = None

    def _reset(self, keys):
        for key in keys:
            default = DEFAULTS[key]
            self.config[key] = list(default) if isinstance(default, list) else default
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()
        self.group_states.clear()

    async def reset_rates(self):
        self._reset(RATE_KEYS)
        return json_response({"message": "已恢复默认概率 (20%、100%、80%、50%)"})

    async def reset_all(self):
        self._reset(DEFAULTS)
        return json_response({"message": "已恢复全部默认设置"})
