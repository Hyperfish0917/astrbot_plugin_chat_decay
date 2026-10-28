"""Low-cost, reply-confirmed group conversation wake/decay for AstrBot >= 4.29.3.

The framework's wake pipeline invokes our group listener for ordinary messages,
but it calls its default LLM only when ``is_at_or_wake_command`` becomes True.
Do not stop unrelated plugin events, and never count an *admitted* request as an
actual reply: an LLM request may fail or intentionally produce no output.
"""

from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register
from astrbot.api.web import json_response

PLUGIN_NAME = "astrbot_plugin_chat_decay"
PENDING_EXTRA = "chat_decay_v14_pending"
PENDING_TIMEOUT_SECONDS = 90
DEFAULTS = {
    "keywords": ["肥鱼", "鲸鱼", "deepseek"],
    "ignore_case": True,
    "base_passive_rate": 0.20,
    "decay_rate_1": 1.00,
    "decay_rate_2": 0.80,
    "decay_rate_3": 0.50,
    "max_turns": 3,
    "cooldown_seconds": 90,
    "session_timeout_seconds": 30,
    "followup_require_cue": True,
    "miss_action": "cooldown",
}
RATE_KEYS = ("base_passive_rate", "decay_rate_1", "decay_rate_2", "decay_rate_3")
# Only lightweight conversational signals are used; this is NOT semantic analysis.
FOLLOWUP_PREFIXES = (
    "那", "所以", "然后", "不过", "但是", "可是", "因为", "为什么", "怎么", "继续",
    "接着", "还有", "这个", "那个", "它", "他", "她", "这些", "那些", "刚才",
    "上面", "前面", "这样", "对", "是的", "是吗", "真的吗", "行", "好", "嗯",
    "哦", "没错", "可以", "能不能", "要不", "你说", "什么意思", "啥意思", "能否",
    "再说", "比如", "举个例子", "如果", "那就", "ok", "okay", "yes", "but", "why",
    "how", "and", "so", "what about", "really", "continue", "tell me more",
)
CUE_RE = re.compile(r"[？?]|(?:吗|呢|吧|么)[？?！!。]*$")


@dataclass
class PendingReply:
    token: int
    speaker: str
    kind: str  # direct / cold / followup
    round_number: int
    started_at: float


@dataclass
class GroupState:
    """Ephemeral state, isolated by platform instance + group + bot."""

    followups_completed: int = 0
    last_reply_at: float | None = None
    last_speaker_id: str = ""
    cooldown_until: float = 0.0
    pending: PendingReply | None = None


@register("chat_decay", "Hyperfish0917", "关键词唤醒与群聊概率衰减", "1.4.0")
class ChatDecayPlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.group_states: dict[str, GroupState] = {}
        self._next_token = 0
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings/reset-rates", self.reset_rates, ["POST"],
            "Reset probability settings",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings/reset-all", self.reset_all, ["POST"],
            "Reset all plugin settings",
        )

    def _get_cfg(self, key):
        val = self.config.get(key, DEFAULTS[key])
        return DEFAULTS[key] if val is None else val

    def _rate(self, key):
        try:
            p = float(self._get_cfg(key))
            return p if math.isfinite(p) and 0 <= p <= 1 else DEFAULTS[key]
        except (ValueError, TypeError, OverflowError):
            return DEFAULTS[key]

    def _bounded_int(self, key, minimum, maximum):
        try:
            return min(max(int(self._get_cfg(key)), minimum), maximum)
        except (ValueError, TypeError, OverflowError):
            return DEFAULTS[key]

    def _miss_action(self):
        val = self._get_cfg("miss_action")
        return val if val in ("cooldown", "baseline") else "cooldown"

    def _keyword_hit(self, message):
        words = self._get_cfg("keywords")
        if not isinstance(words, (tuple, list)) or not message:
            return False
        ignore_case = bool(self._get_cfg("ignore_case"))
        text = message.casefold() if ignore_case else message
        return any(
            ((w.casefold() if ignore_case else w) in text)
            for w in words if isinstance(w, str) and w.strip()
        )

    @staticmethod
    def _direct_poke(event):
        bot_id = str(event.get_self_id() or "")
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

    @staticmethod
    def _is_reply_to_bot(event):
        bot_id = str(event.get_self_id() or "")
        if not bot_id:
            return False
        for part in event.get_messages():
            if part.__class__.__name__ == "Reply":
                if str(getattr(part, "sender_id", "")) == bot_id:
                    return True
        return False

    def _group_key(self, event):
        return f"{event.unified_msg_origin}:{event.get_self_id()}"

    @staticmethod
    def _end_conversation(state, now, cooldown):
        state.followups_completed = 0
        state.last_reply_at = None
        state.last_speaker_id = ""
        state.cooldown_until = now + cooldown if cooldown else 0.0

    def _is_followup(self, state, event, message, now):
        if state.last_reply_at is None:
            return False
        if now - state.last_reply_at > self._bounded_int("session_timeout_seconds", 1, 3600):
            return False
        # QQ Reply to bot is handled as a direct/native wake, even across users.
        if not state.last_speaker_id or state.last_speaker_id != str(event.get_sender_id()):
            return False
        if not bool(self._get_cfg("followup_require_cue")):
            return True
        text = message.strip().casefold()
        return text.startswith(FOLLOWUP_PREFIXES) or bool(CUE_RE.search(text))

    def _admit(self, event, state, kind, round_number, now):
        self._next_token += 1
        pending = PendingReply(
            token=self._next_token, speaker=str(event.get_sender_id() or ""),
            kind=kind, round_number=round_number, started_at=now,
        )
        state.pending = pending
        event.set_extra(PENDING_EXTRA, (self._group_key(event), pending.token))
        event.is_at_or_wake_command = True
        self._watch_streaming_send(event)
        logger.debug("ChatDecay: admitted %s, round=%s; awaiting actual reply", kind, round_number)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=50)
    async def handle_group_message(self, event: AstrMessageEvent):
        if event.get_sender_id() and str(event.get_sender_id()) == str(event.get_self_id()):
            return
        message = (event.message_str or "").strip()
        now = time.monotonic()
        key = self._group_key(event)
        state = self.group_states.setdefault(key, GroupState())
        direct = bool(event.is_at_or_wake_command) or self._keyword_hit(message) or self._direct_poke(event) or self._is_reply_to_bot(event)
        if direct:
            # Slash commands belong to other plugins; never interpret their
            # output as a new LLM conversation without proof of LLM delivery.
            if message.startswith("/"):
                return
            self._admit(event, state, "direct", 0, now)
            return
        if not message or message.startswith("/"):
            return

        if state.pending is not None:
            if now - state.pending.started_at < PENDING_TIMEOUT_SECONDS:
                logger.debug("ChatDecay: waiting for previous reply; skip")
                return
            logger.debug("ChatDecay: discarded expired pending reply")
            state.pending = None
        if now < state.cooldown_until:
            logger.debug("ChatDecay: cooldown active; skip")
            return
        if state.last_reply_at is not None and now - state.last_reply_at > self._bounded_int("session_timeout_seconds", 1, 3600):
            self._end_conversation(state, now, 0)

        is_followup = self._is_followup(state, event, message, now)
        if is_followup:
            round_number = state.followups_completed + 1
            probability = self._rate(f"decay_rate_{min(round_number, 3)}")
            kind = "followup"
        else:
            # Unrelated messages do not inherit elevated follow-up rates.
            if state.last_reply_at is not None:
                self._end_conversation(state, now, 0)
            round_number = 0
            probability = self._rate("base_passive_rate")
            kind = "cold"

        chosen = random.random() < probability
        logger.debug("ChatDecay: %s round=%s probability=%.2f hit=%s", kind, round_number, probability, chosen)
        if not chosen:
            # Baseline misses do not start cooldown. Follow-up misses end the
            # conversation and optionally cool down, rather than retrying 80%.
            if is_followup:
                cooldown = self._bounded_int("cooldown_seconds", 0, 3600) if self._miss_action() == "cooldown" else 0
                self._end_conversation(state, now, cooldown)
            return
        self._admit(event, state, kind, round_number, now)

    def _complete(self, event):
        ref = event.get_extra(PENDING_EXTRA)
        if not (isinstance(ref, tuple) and len(ref) == 2):
            return False
        key, token = ref
        state = self.group_states.get(key)
        if state is None or state.pending is None or state.pending.token != token:
            return False
        pending = state.pending
        state.pending = None
        event.set_extra(PENDING_EXTRA, None)  # idempotent for segmented output
        now = time.monotonic()
        if pending.kind == "direct":
            state.followups_completed = 0
            state.cooldown_until = 0
        elif pending.kind == "cold":
            state.followups_completed = 0
            state.cooldown_until = 0
        else:
            state.followups_completed = pending.round_number
        state.last_reply_at = now
        state.last_speaker_id = pending.speaker
        if state.followups_completed >= self._bounded_int("max_turns", 1, 3):
            self._end_conversation(state, now, self._bounded_int("cooldown_seconds", 0, 3600))
        logger.debug("ChatDecay: reply confirmed (%s); followups=%d", pending.kind, state.followups_completed)
        return True

    @filter.after_message_sent()
    async def on_message_sent(self, event: AstrMessageEvent):
        # Called by AstrBot RespondStage AFTER a non-streaming message send.
        # The hook also runs after a failed adapter send, so require the
        # framework's actual send-operation flag and a successful LLM result.
        if not getattr(event, "_has_send_oper", False):
            return
        result = event.get_result()
        if result is None or not callable(getattr(result, "is_llm_result", None)) or not result.is_llm_result():
            return
        self._complete(event)

    def _watch_streaming_send(self, event):
        """Observe an event-local streaming send *after* its adapter returns.

        AstrBot 4.29.3 does not call after_message_sent for streaming, and
        ResultDecorateStage skips empty-chain STREAMING_RESULT events entirely.
        Wrapping the per-event adapter method is the only reliable local point
        where the completed streaming send operation can be observed without
        monkeypatching an AstrBot class or intercepting unrelated messages.
        """
        original = getattr(event, "send_streaming", None)
        if not callable(original):
            return
        if event.get_extra("chat_decay_v14_stream_watch"):
            return
        event.set_extra("chat_decay_v14_stream_watch", True)

        async def observed_send_streaming(generator, use_fallback=False):
            saw_visible = False

            async def visible_chunks():
                nonlocal saw_visible
                async for chunk in generator:
                    typ = str(getattr(chunk, "type", ""))
                    if typ not in {"break", "reasoning", "tool_call", "agent_stats", "audio_chunk"}:
                        saw_visible = saw_visible or bool(getattr(chunk, "chain", None))
                    yield chunk

            try:
                delivery_result = await original(visible_chunks(), use_fallback)
                if saw_visible and getattr(event, "_has_send_oper", False):
                    self._complete(event)
                return delivery_result
            finally:
                if getattr(event, "send_streaming", None) is observed_send_streaming:
                    event.send_streaming = original

        event.send_streaming = observed_send_streaming

    def _reset(self, keys):
        for key in keys:
            val = DEFAULTS[key]
            self.config[key] = list(val) if isinstance(val, list) else val
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()
        self.group_states.clear()

    async def reset_rates(self):
        self._reset(RATE_KEYS)
        return json_response({"message": "已恢复默认概率 (20%、100%、80%、50%)"})

    async def reset_all(self):
        self._reset(DEFAULTS)
        return json_response({"message": "已恢复全部默认设置 (含接话判断与概率未命中处理)"})
