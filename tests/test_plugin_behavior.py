import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


def _load_plugin_class():
    class FakeMessageChain:
        def message(self, text):
            self.text = text
            return self

    class Decorators:
        def command(self, *args, **kwargs):
            return lambda func: func

        def on_llm_request(self, *args, **kwargs):
            return lambda func: func

    logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    aiohttp = types.ModuleType("aiohttp")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    provider = types.ModuleType("astrbot.api.provider")
    api.logger = logger
    event.filter = Decorators()
    event.AstrMessageEvent = object
    event.MessageChain = FakeMessageChain
    star.Context = object
    star.Star = object
    star.register = lambda *args, **kwargs: (lambda cls: cls)
    provider.ProviderRequest = object
    aiohttp.web = SimpleNamespace()
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.provider": provider,
            "aiohttp": aiohttp,
        }
    )
    repo_parent = str(Path(__file__).resolve().parents[2])
    if repo_parent not in sys.path:
        sys.path.insert(0, repo_parent)
    return importlib.import_module("astrbot_plugin_body_monitor.main").BodyMonitorPlugin


BodyMonitorPlugin = _load_plugin_class()
from astrbot_plugin_body_monitor.body_monitor_api import BodyMonitorEventStore, BodyMonitorExtensionAPI


class BodyMonitorPluginBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_connect_sample_time_is_preserved_for_event_identity(self):
        plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)

        parsed = plugin._parse_health_connect_data(
            {
                "heart_rate": [
                    {"bpm": 118, "time": "2026-07-27T10:00:00Z"}
                ]
            }
        )

        self.assertEqual(118.0, parsed["heart_rate"])
        self.assertEqual("2026-07-27T10:00:00Z", parsed["timestamp"])

    def test_private_companion_integration_defaults_to_disabled(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertFalse(schema["enable_private_companion_integration"]["default"])
        self.assertIn("llm_provider_id", schema)
        self.assertIn("persona_id", schema)

    async def test_enabled_integration_only_persists_event_and_enters_cooldown(self):
        plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
        plugin.enable_private_companion_integration = True
        plugin.metrics_config = {"heart_rate": {"threshold": 2, "cooldown": 4}}
        plugin.alert_cooldown = {}
        plugin._calculate_baseline = lambda metric: (70, 10)
        plugin._is_in_cooldown = lambda metric, hours: False
        plugin._record_proactive_event = Mock()
        plugin._generate_care_message = AsyncMock()
        plugin._send_care_message = AsyncMock()
        plugin._record_legacy_alert = Mock()

        await plugin._check_metric("heart_rate", 100, "2026-07-27T10:00:00+00:00")

        plugin._record_proactive_event.assert_called_once_with(
            "heart_rate", 100, 70, "2026-07-27T10:00:00+00:00"
        )
        plugin._generate_care_message.assert_not_awaited()
        plugin._send_care_message.assert_not_awaited()
        plugin._record_legacy_alert.assert_not_called()
        self.assertIn("heart_rate", plugin.alert_cooldown)

    async def test_disabled_integration_preserves_legacy_llm_send_and_alert(self):
        plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
        plugin.enable_private_companion_integration = False
        plugin.metrics_config = {"heart_rate": {"threshold": 2, "cooldown": 4}}
        plugin.alert_cooldown = {}
        plugin._calculate_baseline = lambda metric: (70, 10)
        plugin._is_in_cooldown = lambda metric, hours: False
        plugin._generate_care_message = AsyncMock(return_value="legacy care")
        plugin._send_care_message = AsyncMock()
        plugin._record_legacy_alert = Mock()
        plugin._record_proactive_event = Mock()

        await plugin._check_metric("heart_rate", 100, "2026-07-27T10:00:00+00:00")

        plugin._generate_care_message.assert_awaited_once_with(
            "heart_rate", 100, 70, 10, 3
        )
        plugin._send_care_message.assert_awaited_once_with("legacy care")
        plugin._record_legacy_alert.assert_called_once_with(
            "heart_rate", 100, 70, 10, 3, "legacy care"
        )
        plugin._record_proactive_event.assert_not_called()
        self.assertIn("heart_rate", plugin.alert_cooldown)

    async def test_enabled_body_test_creates_event_without_sending(self):
        plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
        plugin.enable_private_companion_integration = True
        plugin._get_targets = lambda: ["aiocqhttp:FriendMessage:10001"]
        plugin._get_today_context = lambda: {"steps": 1234}
        plugin._send_care_message = AsyncMock()
        plugin.event_store = SimpleNamespace(record_health_alert=Mock())
        plugin.event_store.record_health_alert.return_value = SimpleNamespace(
            created=True, event_id=7
        )
        event = SimpleNamespace(plain_result=lambda text: text)

        results = [result async for result in plugin.cmd_test(event)]

        call = plugin.event_store.record_health_alert.call_args.kwargs
        self.assertEqual("test", call["metric"])
        self.assertEqual(["aiocqhttp:FriendMessage:10001"], call["targets"])
        self.assertEqual({"steps": 1234}, call["today_context"])
        self.assertIn("测试事件已创建", results[0])
        plugin._send_care_message.assert_not_awaited()

    async def test_disabled_body_test_preserves_direct_send(self):
        plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
        plugin.enable_private_companion_integration = False
        plugin._send_care_message = AsyncMock()
        plugin.event_store = SimpleNamespace(record_health_alert=Mock())
        event = SimpleNamespace(plain_result=lambda text: text)

        results = [result async for result in plugin.cmd_test(event)]

        plugin._send_care_message.assert_awaited_once_with(
            "测试关心消息：记得多喝水，注意休息~"
        )
        plugin.event_store.record_health_alert.assert_not_called()
        self.assertIn("测试消息已发送", results[0])

    async def test_legacy_direct_send_uses_astrbot_message_chain(self):
        plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
        plugin._get_targets = lambda: ["aiocqhttp:FriendMessage:10001"]
        plugin.context = SimpleNamespace(send_message=AsyncMock())

        await plugin._send_care_message("legacy care")

        umo, chain = plugin.context.send_message.await_args.args
        self.assertEqual("aiocqhttp:FriendMessage:10001", umo)
        self.assertEqual("legacy care", chain.text)

    def test_legacy_alert_keeps_original_fields_and_stays_out_of_event_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
            plugin.db_path = str(Path(tmp) / "body_monitor.db")
            BodyMonitorExtensionAPI(plugin.db_path)

            plugin._record_legacy_alert(
                "heart_rate", 100, 70, 10, 3, "legacy care"
            )

            conn = sqlite3.connect(plugin.db_path)
            row = conn.execute(
                "SELECT metric, value, baseline_mean, baseline_std, z_score, "
                "llm_response, event_key FROM alerts"
            ).fetchone()
            conn.close()
            self.assertEqual(
                ("heart_rate", 100.0, 70.0, 10.0, 3.0, "legacy care", None),
                row,
            )
            feed = BodyMonitorExtensionAPI(plugin.db_path).read_proactive_events(
                after_cursor=0
            )
            self.assertEqual([], feed["events"])
            self.assertEqual(1, feed["next_cursor"])

    async def test_upload_and_periodic_paths_share_stable_event_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
            plugin.db_path = str(Path(tmp) / "body_monitor.db")
            plugin.event_store = BodyMonitorEventStore(plugin.db_path)
            plugin.enable_private_companion_integration = True
            plugin.metrics_config = {"heart_rate": {"enabled": True, "threshold": 2, "cooldown": 4}}
            plugin.alert_cooldown = {}
            plugin._calculate_baseline = lambda metric: (70, 10)
            plugin._is_in_cooldown = lambda metric, hours: False
            plugin._get_targets = lambda: ["aiocqhttp:FriendMessage:10001"]
            plugin._get_today_context = lambda: {"steps": 1234}
            plugin._get_body_composition_context = lambda: {}
            plugin._in_quiet_hours = lambda: False
            plugin._is_baseline_ready = lambda: True
            sample = (datetime.now(timezone.utc).replace(microsecond=0).isoformat(), 100)
            plugin._get_latest_metric_record = lambda metric: sample

            await plugin._check_metric("heart_rate", sample[1], sample[0])
            plugin.alert_cooldown.clear()
            await plugin._periodic_check()

            feed = BodyMonitorExtensionAPI(plugin.db_path).read_proactive_events(after_cursor=0)
            self.assertEqual(1, len(feed["events"]))


if __name__ == "__main__":
    unittest.main()
