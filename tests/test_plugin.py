import json
from pathlib import Path

import pytest

from conftest import FakeConfig, FakeContext, FakeEvent


async def run(plugin, event):
    await plugin.handle_group_message(event)
    assert event.stop_calls == 0
    return event.is_at_or_wake_command


@pytest.mark.asyncio
async def test_keyword_bypasses_probability_and_cooldown(plugin, roll):
    roll["random"] = 1
    await run(plugin, FakeEvent("鲸鱼你好"))
    key = "adapterA:group:1:9"
    assert plugin.group_states[key].cooldown_until == 0
    plugin.group_states[key].cooldown_until = 999
    event = FakeEvent("看 DEEPSEEK 的观点")
    assert await run(plugin, event)
    assert plugin.group_states[key].cooldown_until == 0


@pytest.mark.asyncio
async def test_native_wake_is_preserved(plugin, roll):
    roll["random"] = 1
    assert await run(plugin, FakeEvent("/help", native=True))
    assert not await run(plugin, FakeEvent("/help"))


@pytest.mark.asyncio
async def test_baseline_probability(plugin, roll):
    roll["random"] = 0.049
    assert await run(plugin, FakeEvent())
    roll["random"] = 0.051
    assert not await run(plugin, FakeEvent(group="new"))


@pytest.mark.asyncio
async def test_decay_60_25_8_and_cooldown(plugin, roll):
    assert await run(plugin, FakeEvent("肥鱼"))
    key = "adapterA:group:1:9"
    roll["random"] = .59
    roll["now"] += 1
    assert await run(plugin, FakeEvent())
    assert plugin.group_states[key].accepted_turns == 1
    roll["random"] = .26
    roll["now"] += 1
    assert not await run(plugin, FakeEvent())
    assert plugin.group_states[key].accepted_turns == 1
    roll["random"] = .24
    roll["now"] += 1
    assert await run(plugin, FakeEvent())
    assert plugin.group_states[key].accepted_turns == 2
    roll["random"] = .09
    roll["now"] += 1
    assert not await run(plugin, FakeEvent())
    roll["random"] = .079
    roll["now"] += 1
    assert await run(plugin, FakeEvent())
    assert plugin.group_states[key].cooldown_until == roll["now"] + 120
    roll["random"] = 0
    roll["now"] += 1
    assert not await run(plugin, FakeEvent())
    roll["now"] += 120
    roll["random"] = .051
    assert not await run(plugin, FakeEvent())


@pytest.mark.asyncio
async def test_cold_start_then_60_not_25(plugin, roll):
    assert await run(plugin, FakeEvent())
    roll["now"] += 1
    roll["random"] = .59
    assert await run(plugin, FakeEvent())
    assert plugin.group_states["adapterA:group:1:9"].accepted_turns == 1


@pytest.mark.asyncio
async def test_timeout_resets_to_baseline(plugin, roll):
    await run(plugin, FakeEvent("鲸鱼"))
    roll["now"] += 46
    roll["random"] = .25
    assert not await run(plugin, FakeEvent())


@pytest.mark.asyncio
async def test_timeout_just_before_45_sec(plugin, roll):
    await run(plugin, FakeEvent("鲸鱼"))
    roll["now"] += 45
    roll["random"] = .59
    assert await run(plugin, FakeEvent())


@pytest.mark.asyncio
@pytest.mark.parametrize("rate,expected", [(-1,.05), (2,.05), ("nan", .05), ("hello", .05), (None,.05), (0,0), (1,1)])
async def test_probability_validation(plugin, roll, rate, expected):
    plugin.config["base_passive_rate"] = rate
    roll["random"] = .5
    assert await run(plugin, FakeEvent()) is (expected == 1)


@pytest.mark.asyncio
async def test_custom_rates(plugin, roll):
    plugin.config["decay_rate_1"] = .9
    await run(plugin, FakeEvent("肥鱼"))
    roll["random"] = .8
    roll["now"] += 1
    assert await run(plugin, FakeEvent())


@pytest.mark.asyncio
async def test_max_turns_one(plugin, roll):
    plugin.config["max_turns"] = 1
    await run(plugin, FakeEvent("肥鱼"))
    roll["now"] += 1
    assert await run(plugin, FakeEvent())
    assert plugin.group_states["adapterA:group:1:9"].cooldown_until > roll["now"]


@pytest.mark.asyncio
async def test_group_isolation(plugin, roll):
    await run(plugin, FakeEvent("肥鱼", group="1"))
    roll["random"] = .1
    assert not await run(plugin, FakeEvent(group="2"))
    assert await run(plugin, FakeEvent(group="1"))


@pytest.mark.asyncio
async def test_adapter_and_bot_isolation(plugin, roll):
    await run(plugin, FakeEvent("肥鱼"))
    roll["random"] = .1
    assert not await run(plugin, FakeEvent(adapter="adapterB"))
    assert not await run(plugin, FakeEvent(bot="10"))


@pytest.mark.asyncio
async def test_own_message_not_activated(plugin, roll):
    assert not await run(plugin, FakeEvent("肥鱼", sender="9"))
    assert plugin.group_states == {}


@pytest.mark.asyncio
async def test_empty_message_not_activated(plugin, roll):
    assert not await run(plugin, FakeEvent(" "))


@pytest.mark.asyncio
async def test_poke_targets_own_bot(plugin, roll):
    class Poke:
        target_id = "9"
    class OtherPoke:
        pass
    roll["random"] = .99
    assert await run(plugin, FakeEvent("", parts=[Poke()]))
    assert not await run(plugin, FakeEvent("", parts=[OtherPoke()]))
    class RealLikePoke:
        def target_id(self):
            return "9"
    RealLikePoke.__name__ = "Poke"
    assert await run(plugin, FakeEvent("", parts=[RealLikePoke()]))


@pytest.mark.asyncio
async def test_keyword_case_sensitive(plugin, roll):
    plugin.config["ignore_case"] = False
    roll["random"] = .99
    assert not await run(plugin, FakeEvent("DEEPSEEK"))
    assert await run(plugin, FakeEvent("deepseek"))


@pytest.mark.asyncio
async def test_empty_keywords(plugin, roll):
    plugin.config["keywords"] = []
    roll["random"] = .99
    assert not await run(plugin, FakeEvent("肥鱼"))


@pytest.mark.asyncio
async def test_malformed_keywords(plugin, roll):
    plugin.config["keywords"] = [None, 3, ""]
    roll["random"] = .99
    assert not await run(plugin, FakeEvent("肥鱼"))


@pytest.mark.asyncio
async def test_reset_rates_preserves_other_settings(plugin, module, roll):
    plugin.config["keywords"] = ["自定义"]
    plugin.config["decay_rate_2"] = .99
    plugin.config["base_passive_rate"] = .8
    await run(plugin, FakeEvent("自定义"))
    result = await plugin.reset_rates()
    assert "默认概率" in result["message"]
    assert plugin.config["keywords"] == ["自定义"]
    assert plugin.config["decay_rate_2"] == .25
    assert plugin.config["base_passive_rate"] == .05
    assert plugin.group_states == {}
    assert plugin.config.saves == 1


@pytest.mark.asyncio
async def test_reset_all(plugin, module, roll):
    plugin.config.update(keywords=["自定义"], cooldown_seconds=999, decay_rate_1=.9)
    await plugin.reset_all()
    for key, val in module.DEFAULTS.items():
        assert plugin.config[key] == val
    assert plugin.config.saves == 1
    plugin.config["keywords"].append("不影响常量")
    assert module.DEFAULTS["keywords"] == ["肥鱼", "鲸鱼", "deepseek"]


def test_routes_registered(plugin):
    assert len(plugin.context.routes) == 2
    assert all(v[1] == ["POST"] for v in plugin.context.routes.values())


def test_schema_matches_defaults(module):
    schema = json.loads((Path(__file__).resolve().parents[1]/"_conf_schema.json").read_text())
    assert set(schema) == set(module.DEFAULTS)
    assert all(schema[key]["default"] == val for key, val in module.DEFAULTS.items())


def test_metadata_contains_public_repo():
    content = (Path(__file__).resolve().parents[1]/"metadata.yaml").read_text()
    assert "https://github.com/Hyperfish0917/astrbot_plugin_chat_decay" in content
    assert "1.2.0" in content
