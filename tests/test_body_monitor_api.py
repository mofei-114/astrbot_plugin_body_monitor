import sqlite3
import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from body_monitor_api import (
    BodyMonitorEventStore,
    BodyMonitorExtensionAPI,
    get_body_monitor_api,
    register_body_monitor_api,
    unregister_body_monitor_api,
)


class BodyMonitorExtensionAPITests(unittest.TestCase):
    def test_public_getter_exposes_only_the_registered_read_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = BodyMonitorExtensionAPI(str(Path(tmp) / "body_monitor.db"))

            register_body_monitor_api(api)
            self.assertIs(api, get_body_monitor_api())
            unregister_body_monitor_api(api)
            self.assertIsNone(get_body_monitor_api())

    def test_initialization_migrates_legacy_alerts_without_replaying_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "body_monitor.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    metric TEXT,
                    value REAL,
                    baseline_mean REAL,
                    baseline_std REAL,
                    z_score REAL,
                    llm_response TEXT,
                    resolved INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT INTO alerts
                    (timestamp, metric, value, baseline_mean, baseline_std, z_score, llm_response)
                VALUES ('2026-07-27T10:00:00', 'heart_rate', 120, 70, 10, 5, 'legacy')
                """
            )
            conn.commit()
            conn.close()

            api = BodyMonitorExtensionAPI(str(db_path))
            initialized = api.read_proactive_events(after_cursor=None)
            repeated = api.read_proactive_events(after_cursor=None)

            self.assertEqual(1, initialized["version"])
            self.assertEqual([], initialized["events"])
            self.assertEqual(1, initialized["next_cursor"])
            self.assertEqual(1, initialized["latest_cursor"])
            self.assertFalse(initialized["has_more"])
            self.assertEqual(initialized["stream_id"], repeated["stream_id"])

            conn = sqlite3.connect(db_path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
            legacy_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            conn.close()

            self.assertEqual(1, legacy_count)
            self.assertTrue(
                {"event_key", "expires_at", "targets_json", "context_json", "severity", "topic"}
                <= columns
            )

    def test_feed_scans_all_rows_but_only_projects_live_canonical_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "body_monitor.db"
            api = BodyMonitorExtensionAPI(str(db_path))
            now = datetime.now(timezone.utc)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO alerts (timestamp, metric, value) VALUES (?, ?, ?)",
                (now.isoformat(), "heart_rate", 100),
            )
            conn.execute(
                """
                INSERT INTO alerts
                    (timestamp, metric, value, event_key, expires_at, targets_json,
                     context_json, severity, topic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (now - timedelta(hours=1)).isoformat(),
                    "spo2",
                    91,
                    "expired-event",
                    (now - timedelta(minutes=30)).isoformat(),
                    json.dumps(["qq_private:1"]),
                    json.dumps({"metric": "spo2", "value": 91, "baseline": {"mean": 97}}),
                    "warning",
                    "血氧变化",
                ),
            )
            conn.execute(
                """
                INSERT INTO alerts
                    (timestamp, metric, value, event_key, expires_at, targets_json,
                     context_json, severity, topic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(),
                    "heart_rate",
                    118,
                    "live-event",
                    (now + timedelta(minutes=30)).isoformat(),
                    json.dumps(["qq_private:1", "qq_private:2"]),
                    json.dumps(
                        {
                            "metric": "heart_rate",
                            "value": 118,
                            "baseline": {"mean": 72},
                            "today": {"steps": 3210},
                        }
                    ),
                    "warning",
                    "心率变化",
                ),
            )
            conn.commit()
            conn.close()

            first = api.read_proactive_events(after_cursor=0, limit=2)
            second = api.read_proactive_events(after_cursor=first["next_cursor"], limit=2)

            self.assertEqual([], first["events"])
            self.assertEqual(2, first["next_cursor"])
            self.assertEqual(3, first["latest_cursor"])
            self.assertTrue(first["has_more"])
            self.assertEqual(3, second["next_cursor"])
            self.assertFalse(second["has_more"])
            self.assertEqual(
                {
                    "id": 3,
                    "event_key": "live-event",
                    "type": "health_alert",
                    "occurred_at": now.isoformat(),
                    "expires_at": (now + timedelta(minutes=30)).isoformat(),
                    "severity": "warning",
                    "topic": "心率变化",
                    "targets": ["qq_private:1", "qq_private:2"],
                    "context": {
                        "metric": "heart_rate",
                        "value": 118,
                        "baseline": {"mean": 72},
                        "today": {"steps": 3210},
                    },
                },
                second["events"][0],
            )

    def test_recording_is_idempotent_and_snapshots_canonical_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "body_monitor.db"
            store = BodyMonitorEventStore(str(db_path))
            api = BodyMonitorExtensionAPI(str(db_path))
            occurred_at = datetime.now(timezone.utc).replace(microsecond=0)

            first = store.record_health_alert(
                metric="heart_rate",
                value=118.0,
                baseline_mean=72.5,
                occurred_at=occurred_at,
                targets=["qq_private:1"],
                today_context={"steps": 4321, "raw_json": "must not leak"},
            )
            duplicate = store.record_health_alert(
                metric="heart_rate",
                value=118,
                baseline_mean=72.5,
                occurred_at=occurred_at,
                targets=["qq_private:changed"],
                today_context={"steps": 9999},
            )
            feed = api.read_proactive_events(after_cursor=0)

            self.assertTrue(first.created)
            self.assertFalse(duplicate.created)
            self.assertEqual(first.event_key, duplicate.event_key)
            self.assertEqual(1, len(feed["events"]))
            event = feed["events"][0]
            self.assertEqual(["qq_private:1"], event["targets"])
            self.assertEqual("心率变化", event["topic"])
            self.assertEqual(
                {
                    "metric": "heart_rate",
                    "value": 118.0,
                    "baseline": {"mean": 72.5},
                    "today": {"steps": 4321},
                },
                event["context"],
            )
            self.assertEqual(
                occurred_at + timedelta(minutes=30),
                datetime.fromisoformat(event["expires_at"]),
            )

    def test_database_rebuild_changes_stream_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "body_monitor.db"
            first_stream = BodyMonitorExtensionAPI(str(db_path)).read_proactive_events(
                after_cursor=None
            )["stream_id"]
            db_path.unlink()
            second_stream = BodyMonitorExtensionAPI(str(db_path)).read_proactive_events(
                after_cursor=None
            )["stream_id"]

            self.assertNotEqual(first_stream, second_stream)


if __name__ == "__main__":
    unittest.main()
