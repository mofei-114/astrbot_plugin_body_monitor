import importlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def _load_plugin_class():
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

    async def test_anomaly_detection_only_persists_event_and_enters_cooldown(self):
        plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
        plugin.metrics_config = {"heart_rate": {"threshold": 2, "cooldown": 4}}
        plugin.alert_cooldown = {}
        plugin._calculate_baseline = lambda metric: (70, 10)
        plugin._is_in_cooldown = lambda metric, hours: False
        plugin._record_alert = Mock()

        await plugin._check_metric("heart_rate", 100, "2026-07-27T10:00:00+00:00")

        plugin._record_alert.assert_called_once_with(
            "heart_rate", 100, 70, "2026-07-27T10:00:00+00:00"
        )
        self.assertIn("heart_rate", plugin.alert_cooldown)
        self.assertFalse(hasattr(BodyMonitorPlugin, "_generate_care_message"))
        self.assertFalse(hasattr(BodyMonitorPlugin, "_send_care_message"))

    async def test_body_test_creates_event_without_sending(self):
        plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
        plugin._get_targets = lambda: ["aiocqhttp:FriendMessage:10001"]
        plugin._get_today_context = lambda: {"steps": 1234}
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

    async def test_upload_and_periodic_paths_share_stable_event_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = BodyMonitorPlugin.__new__(BodyMonitorPlugin)
            plugin.db_path = str(Path(tmp) / "body_monitor.db")
            plugin.event_store = BodyMonitorEventStore(plugin.db_path)
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
