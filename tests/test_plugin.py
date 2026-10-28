import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeEvent, FakeResult


async def incoming(plugin, text="普通消息", **kw):
    event = FakeEvent(text, **kw)
    await plugin.handle_group_message(event)
    assert event.stop_calls == 0
    return event


async def delivered(plugin, event, model=True, sent=True):
    event.result = FakeResult(model=model)
    event._has_send_oper = sent
    await plugin.on_message_sent(event)


async def replied(plugin, text="普通消息", **kw):
    e = await incoming(plugin, text, **kw)
    assert e.is_at_or_wake_command
    await delivered(plugin, e)
    return e


def state(plugin, group="1", adapter="adapterA", bot="9"):
    return plugin.group_states[f"{adapter}:group:{group}:{bot}"]


@pytest.mark.asyncio
async def test_baseline_20_percent_and_delayed_commit(plugin, roll):
    roll["random"] = .19
    e = await incoming(plugin)
    assert e.is_at_or_wake_command
    assert state(plugin).last_reply_at is None
    await delivered(plugin, e)
    assert state(plugin).last_reply_at == 100


@pytest.mark.asyncio
async def test_baseline_miss_does_not_start_cooldown(plugin, roll):
    roll["random"] = .21
    for _ in range(3):
        assert not (await incoming(plugin)).is_at_or_wake_command
    assert state(plugin).cooldown_until == 0
    roll["random"] = .19
    assert (await incoming(plugin)).is_at_or_wake_command


@pytest.mark.asyncio
async def test_complete_100_80_50_then_cooldown(plugin, roll):
    await replied(plugin, "肥鱼")
    for round_n, rng in [(1, .99), (2, .79), (3, .49)]:
        roll["now"] += 1
        roll["random"] = rng
        e = await incoming(plugin, "那然后呢？")
        assert e.is_at_or_wake_command
        assert state(plugin).followups_completed == round_n - 1
        await delivered(plugin, e)
        if round_n < 3:
            assert state(plugin).followups_completed == round_n
    assert state(plugin).cooldown_until == roll["now"] + 90
    roll["now"] += 1
    roll["random"] = 0
    assert not (await incoming(plugin, "那还有呢")).is_at_or_wake_command
    roll["now"] += 90
    roll["random"] = .21
    assert not (await incoming(plugin)).is_at_or_wake_command


@pytest.mark.asyncio
async def test_second_round_failure_cools_no_retry(plugin, roll):
    await replied(plugin, "肥鱼")
    await replied(plugin, "那继续")
    roll["now"] += 1
    roll["random"] = .81
    assert not (await incoming(plugin, "所以呢")).is_at_or_wake_command
    assert state(plugin).last_reply_at is None
    assert state(plugin).cooldown_until == roll["now"] + 90
    roll["now"] += 1
    roll["random"] = 0
    assert not (await incoming(plugin, "那再说说")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_third_round_failure_cools(plugin, roll):
    await replied(plugin, "肥鱼")
    await replied(plugin, "那继续")
    await replied(plugin, "所以呢")
    roll["random"] = .51
    assert not (await incoming(plugin, "那后来呢")).is_at_or_wake_command
    assert state(plugin).cooldown_until == roll["now"] + 90


@pytest.mark.asyncio
async def test_failure_can_choose_immediate_baseline(plugin, roll):
    plugin.config["miss_action"] = "baseline"
    await replied(plugin, "肥鱼")
    await replied(plugin, "那继续")
    roll["random"] = .81
    assert not (await incoming(plugin, "所以呢")).is_at_or_wake_command
    assert state(plugin).cooldown_until == 0
    roll["random"] = .19
    assert (await incoming(plugin, "新的话题")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_invalid_failure_mode_defaults_cooldown(plugin, roll):
    plugin.config["miss_action"] = "junk"
    await replied(plugin, "肥鱼")
    await replied(plugin, "那继续")
    roll["random"] = .81
    await incoming(plugin, "所以呢")
    assert state(plugin).cooldown_until == 190


@pytest.mark.asyncio
async def test_unrelated_same_user_baseline(plugin, roll):
    await replied(plugin, "肥鱼")
    roll["random"] = .30
    assert not (await incoming(plugin, "明天开会几点开始")).is_at_or_wake_command
    assert state(plugin).last_reply_at is None
    assert state(plugin).cooldown_until == 0


@pytest.mark.asyncio
async def test_other_user_baseline_even_with_cues(plugin, roll):
    await replied(plugin, "肥鱼", sender="2")
    roll["random"] = .30
    assert not (await incoming(plugin, "那后来呢", sender="3")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_same_user_with_cue_inherits_followup(plugin, roll):
    await replied(plugin, "肥鱼")
    roll["random"] = .99
    assert (await incoming(plugin, "那怎么样")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_disable_cue_requirement(plugin, roll):
    plugin.config["followup_require_cue"] = False
    await replied(plugin, "肥鱼")
    roll["random"] = .99
    assert (await incoming(plugin, "明天开会几点开始")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_session_timeout_resets_to_baseline(plugin, roll):
    await replied(plugin, "肥鱼")
    roll["now"] += 31
    roll["random"] = .3
    assert not (await incoming(plugin, "那继续讲")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_timeout_boundary_is_inclusive(plugin, roll):
    await replied(plugin, "肥鱼")
    roll["now"] += 30
    roll["random"] = .99
    assert (await incoming(plugin, "那继续讲")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_timeout_measured_from_real_reply(plugin, roll):
    e = await incoming(plugin, "肥鱼")
    roll["now"] += 50
    await delivered(plugin, e)
    roll["now"] += 29
    roll["random"] = .99
    assert (await incoming(plugin, "所以呢")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_prevent_overlap_while_pending(plugin, roll):
    e = await incoming(plugin, "肥鱼")
    assert state(plugin).last_reply_at is None
    assert not (await incoming(plugin, "那继续")).is_at_or_wake_command
    await delivered(plugin, e, sent=False)
    assert state(plugin).last_reply_at is None


@pytest.mark.asyncio
async def test_non_model_message_not_counted(plugin, roll):
    e = await incoming(plugin, "肥鱼")
    await delivered(plugin, e, model=False)
    assert state(plugin).last_reply_at is None


@pytest.mark.asyncio
async def test_send_hook_is_idempotent(plugin, roll):
    e = await incoming(plugin, "肥鱼")
    await delivered(plugin, e)
    roll["now"] += 10
    await delivered(plugin, e)
    assert state(plugin).last_reply_at == 100


@pytest.mark.asyncio
async def test_abandoned_pending_expires(plugin, roll):
    await incoming(plugin, "肥鱼")
    roll["now"] += 91
    roll["random"] = .19
    assert (await incoming(plugin)).is_at_or_wake_command


@pytest.mark.asyncio
async def test_direct_override_old_pending_callback_ignored(plugin, roll):
    old = await incoming(plugin, "肥鱼")
    new = await incoming(plugin, "deepseek")
    await delivered(plugin, old)
    assert state(plugin).last_reply_at is None
    await delivered(plugin, new)
    assert state(plugin).last_reply_at == roll["now"]


@pytest.mark.asyncio
async def test_direct_bypass_cooldown_but_only_reset_after_reply(plugin, roll, module):
    s = plugin.group_states.setdefault("adapterA:group:1:9", module.GroupState())
    s.cooldown_until = 300
    e = await incoming(plugin, "鲸鱼出来")
    assert e.is_at_or_wake_command
    assert s.cooldown_until == 300
    await delivered(plugin, e)
    assert s.cooldown_until == 0


@pytest.mark.asyncio
async def test_empty_and_slash_commands_are_ignored(plugin, roll):
    assert not (await incoming(plugin, "  ")).is_at_or_wake_command
    assert not (await incoming(plugin, "/help")).is_at_or_wake_command
    assert not (await incoming(plugin, "/help", native=True)).get_extra("chat_decay_v14_pending")


@pytest.mark.asyncio
async def test_native_wake_preserved(plugin, roll):
    roll["random"] = .99
    e = await incoming(plugin, "原生消息", native=True)
    assert e.is_at_or_wake_command
    await delivered(plugin, e)
    assert state(plugin).last_reply_at == 100


@pytest.mark.asyncio
async def test_reply_to_bot_is_direct(plugin, roll):
    class Reply:
        sender_id = "9"
    e = await incoming(plugin, "好", parts=[Reply()])
    assert e.is_at_or_wake_command
    await delivered(plugin, e)


@pytest.mark.asyncio
async def test_reply_to_other_not_direct(plugin, roll):
    class Reply:
        sender_id = "10"
    roll["random"] = .99
    assert not (await incoming(plugin, "普通消息", parts=[Reply()])).is_at_or_wake_command


@pytest.mark.asyncio
async def test_poke_to_bot_is_direct(plugin, roll):
    class Poke:
        def target_id(self):
            return "9"
    roll["random"] = .99
    assert (await incoming(plugin, "", parts=[Poke()])).is_at_or_wake_command


@pytest.mark.asyncio
async def test_poke_to_other_not_direct(plugin, roll):
    class Poke:
        qq = "100"
    roll["random"] = .99
    assert not (await incoming(plugin, "", parts=[Poke()])).is_at_or_wake_command


@pytest.mark.asyncio
async def test_self_message_ignored(plugin, roll):
    assert not (await incoming(plugin, "肥鱼", sender="9")).is_at_or_wake_command
    assert plugin.group_states == {}


@pytest.mark.asyncio
async def test_keyword_case_insensitive(plugin, roll):
    roll["random"] = .99
    assert (await incoming(plugin, "DEEPSEEK 是什么")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_keyword_case_sensitive_option(plugin, roll):
    plugin.config["ignore_case"] = False
    roll["random"] = .99
    assert not (await incoming(plugin, "DEEPSEEK", group="A")).is_at_or_wake_command
    assert (await incoming(plugin, "deepseek", group="B")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_keyword_list_validation(plugin, roll):
    plugin.config["keywords"] = [None, 3, ""]
    roll["random"] = .99
    assert not (await incoming(plugin, "肥鱼")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_group_isolation(plugin, roll):
    await replied(plugin, "肥鱼", group="1")
    roll["random"] = .25
    assert not (await incoming(plugin, "那继续", group="2")).is_at_or_wake_command
    assert (await incoming(plugin, "那继续", group="1")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_adapter_and_bot_isolation(plugin, roll):
    await replied(plugin, "肥鱼", group="1")
    roll["random"] = .3
    assert not (await incoming(plugin, "那继续", adapter="B")).is_at_or_wake_command
    assert not (await incoming(plugin, "那继续", bot="X")).is_at_or_wake_command


@pytest.mark.asyncio
async def test_max_turns_one(plugin, roll):
    plugin.config["max_turns"] = 1
    await replied(plugin, "肥鱼")
    await replied(plugin, "那继续")
    assert state(plugin).cooldown_until == 190


@pytest.mark.asyncio
async def test_reset_rates_preserves_other_settings(plugin, roll):
    plugin.config.update(keywords=["自定义"], base_passive_rate=.9, decay_rate_2=.1, miss_action="baseline")
    await incoming(plugin, "自定义")
    res = await plugin.reset_rates()
    assert "20%、100%、80%、50%" in res["message"]
    assert plugin.config["base_passive_rate"] == .2
    assert plugin.config["decay_rate_2"] == .8
    assert plugin.config["keywords"] == ["自定义"]
    assert plugin.config["miss_action"] == "baseline"
    assert not plugin.group_states
    assert plugin.config.saves == 1


@pytest.mark.asyncio
async def test_reset_all_restores_new_options(plugin, module):
    plugin.config.update(keywords=["x"], miss_action="baseline", followup_require_cue=False)
    await plugin.reset_all()
    for key, default in module.DEFAULTS.items():
        assert plugin.config[key] == default
    plugin.config["keywords"].append("custom")
    assert module.DEFAULTS["keywords"] == ["肥鱼", "鲸鱼", "deepseek"]


@pytest.mark.asyncio
async def test_stream_counts_only_after_adapter_finishes(plugin, roll):
    e = FakeEvent("肥鱼")
    async def fake_stream_send(generator, fallback):
        assert state(plugin).last_reply_at is None
        parts = [p async for p in generator]
        assert len(parts) == 2
        e._has_send_oper = True
    e.send_streaming = fake_stream_send
    await plugin.handle_group_message(e)
    async def chunks():
        yield SimpleNamespace(type="plain", chain=["你好"])
        yield SimpleNamespace(type="break", chain=[])
    await e.send_streaming(chunks(), False)
    assert state(plugin).last_reply_at == roll["now"]
    assert e.send_streaming is fake_stream_send


@pytest.mark.asyncio
async def test_stream_with_no_visible_chunks_not_counted(plugin, roll):
    e = FakeEvent("肥鱼")
    async def fake_stream_send(generator, fallback):
        _ = [p async for p in generator]
        e._has_send_oper = True
    e.send_streaming = fake_stream_send
    await plugin.handle_group_message(e)
    async def chunks():
        yield SimpleNamespace(type="reasoning", chain=["thinking"])
    await e.send_streaming(chunks(), False)
    assert state(plugin).last_reply_at is None


@pytest.mark.asyncio
async def test_stream_adapter_error_does_not_count(plugin, roll):
    e = FakeEvent("肥鱼")
    async def fake_stream_send(generator, fallback):
        _ = [p async for p in generator]
        raise RuntimeError("platform failed")
    e.send_streaming = fake_stream_send
    await plugin.handle_group_message(e)
    async def chunks():
        yield SimpleNamespace(type="plain", chain=["hi"])
    with pytest.raises(RuntimeError):
        await e.send_streaming(chunks(), False)
    assert state(plugin).last_reply_at is None
    assert e.send_streaming is fake_stream_send


@pytest.mark.parametrize("rate,expected", [(-1,.2),(2,.2),("nan",.2),("bad",.2),(None,.2),(0,0),(1,1)])
def test_rate_validation(plugin, rate, expected):
    plugin.config["base_passive_rate"] = rate
    assert plugin._rate("base_passive_rate") == expected


def test_registered_routes(plugin):
    assert len(plugin.context.routes) == 2
    assert all(route[1] == ["POST"] for route in plugin.context.routes.values())


def test_schema_defaults_and_options_match_code(module):
    schema = json.loads((Path(__file__).resolve().parents[1]/"_conf_schema.json").read_text())
    assert set(schema) == set(module.DEFAULTS)
    assert all(schema[k]["default"] == value for k, value in module.DEFAULTS.items())
    assert schema["miss_action"]["options"] == ["cooldown", "baseline"]


def test_metadata_version_and_public_repository():
    text = (Path(__file__).resolve().parents[1]/"metadata.yaml").read_text()
    assert "1.4.0" in text
    assert "https://github.com/Hyperfish0917/astrbot_plugin_chat_decay" in text
