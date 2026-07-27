"""Read-only proactive event feed exposed to companion plugins."""

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Optional


PROACTIVE_EVENT_API_VERSION = 1
_STREAM_ID_KEY = "proactive_event_stream_id"
_active_api: Optional["BodyMonitorExtensionAPI"] = None
_active_api_lock = RLock()
_LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
_TOPICS = {
    "heart_rate": "心率变化",
    "sleep_score": "睡眠状态变化",
    "spo2": "血氧变化",
    "stress": "压力状态变化",
    "test": "联动测试",
}
_SUPPORTED_METRICS = frozenset(_TOPICS)
_TODAY_CONTEXT_KEYS = ("steps", "sleep_score", "spo2", "weight_change")


@dataclass(frozen=True)
class EventRecordResult:
    created: bool
    event_id: int
    event_key: str


class BodyMonitorExtensionAPI:
    """Small read-only interface over Body Monitor's persisted event stream."""

    proactive_event_api_version = PROACTIVE_EVENT_API_VERSION

    def __init__(self, db_path: str, *, proactive_events_enabled: bool = True):
        self._db_path = str(db_path)
        self._proactive_events_enabled = bool(proactive_events_enabled)
        self._lock = RLock()
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize_schema(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
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
            existing = {
                row["name"] for row in conn.execute("PRAGMA table_info(alerts)")
            }
            additions = {
                "event_key": "TEXT",
                "expires_at": "TEXT",
                "targets_json": "TEXT",
                "context_json": "TEXT",
                "severity": "TEXT",
                "topic": "TEXT",
            }
            for name, sql_type in additions.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {sql_type}")
            conn.execute(
                """
                UPDATE alerts
                SET event_key = NULL
                WHERE event_key IS NOT NULL
                  AND id NOT IN (
                      SELECT MIN(id) FROM alerts
                      WHERE event_key IS NOT NULL
                      GROUP BY event_key
                  )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_event_key "
                "ON alerts(event_key) WHERE event_key IS NOT NULL"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS body_monitor_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT value FROM body_monitor_metadata WHERE key = ?",
                (_STREAM_ID_KEY,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO body_monitor_metadata (key, value) VALUES (?, ?)",
                    (_STREAM_ID_KEY, str(uuid.uuid4())),
                )

    def read_proactive_events(
        self, *, after_cursor: int | None, limit: int = 32
    ) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        limit = min(limit, 256)
        with self._lock, self._connection() as conn:
            stream_id = conn.execute(
                "SELECT value FROM body_monitor_metadata WHERE key = ?",
                (_STREAM_ID_KEY,),
            ).fetchone()["value"]
            latest_cursor = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS cursor FROM alerts"
            ).fetchone()["cursor"]

            rows = []
            if after_cursor is not None:
                rows = conn.execute(
                    "SELECT * FROM alerts WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (int(after_cursor), limit),
                ).fetchall()

        if after_cursor is None or not self._proactive_events_enabled:
            return {
                "version": PROACTIVE_EVENT_API_VERSION,
                "stream_id": stream_id,
                "next_cursor": latest_cursor,
                "latest_cursor": latest_cursor,
                "has_more": False,
                "events": [],
            }

        next_cursor = rows[-1]["id"] if rows else int(after_cursor)
        events = []
        now = datetime.now(timezone.utc)
        for row in rows:
            event = _project_event(row, now=now)
            if event is not None:
                events.append(event)

        return {
            "version": PROACTIVE_EVENT_API_VERSION,
            "stream_id": stream_id,
            "next_cursor": next_cursor,
            "latest_cursor": latest_cursor,
            "has_more": next_cursor < latest_cursor,
            "events": events,
        }


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_LOCAL_TIMEZONE).astimezone(timezone.utc)
    return parsed.astimezone(timezone.utc)


def _project_event(row: sqlite3.Row, *, now: datetime) -> dict[str, Any] | None:
    try:
        if not row["event_key"] or not row["context_json"] or not row["expires_at"]:
            return None
        if _parse_datetime(row["expires_at"]) <= now:
            return None
        targets = json.loads(row["targets_json"])
        context = json.loads(row["context_json"])
        if not isinstance(targets, list) or not all(
            isinstance(target, str) and target for target in targets
        ):
            return None
        context = _canonical_context(context, expected_metric=row["metric"])
        if context is None:
            return None
        if row["severity"] not in {"info", "warning", "critical"}:
            return None
        if not isinstance(row["topic"], str) or not row["topic"].strip():
            return None
        _parse_datetime(row["timestamp"])
        return {
            "id": row["id"],
            "event_key": row["event_key"],
            "type": "health_alert",
            "occurred_at": row["timestamp"],
            "expires_at": row["expires_at"],
            "severity": row["severity"],
            "topic": row["topic"],
            "targets": targets,
            "context": context,
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _canonical_context(
    value: Any, *, expected_metric: str
) -> dict[str, Any] | None:
    if expected_metric not in _SUPPORTED_METRICS:
        return None
    if not isinstance(value, dict) or set(value) != {
        "metric",
        "value",
        "baseline",
        "today",
    }:
        return None
    if value["metric"] != expected_metric or not isinstance(value["metric"], str):
        return None
    if (
        isinstance(value["value"], bool)
        or not isinstance(value["value"], (int, float))
        or not math.isfinite(value["value"])
    ):
        return None
    baseline = value["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != {"mean"}:
        return None
    if (
        isinstance(baseline["mean"], bool)
        or not isinstance(baseline["mean"], (int, float))
        or not math.isfinite(baseline["mean"])
    ):
        return None
    result = {
        "metric": value["metric"],
        "value": value["value"],
        "baseline": {"mean": baseline["mean"]},
    }
    today = value["today"]
    if not isinstance(today, dict) or set(today) - set(_TODAY_CONTEXT_KEYS):
        return None
    if not all(_is_safe_context_value(item) for item in today.values()):
        return None
    result["today"] = dict(today)
    return result


def _is_safe_context_value(value: Any) -> bool:
    if isinstance(value, str):
        return True
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


class BodyMonitorEventStore:
    """Internal write interface used by the Body Monitor plugin."""

    def __init__(self, db_path: str):
        self._api = BodyMonitorExtensionAPI(db_path)

    def record_health_alert(
        self,
        *,
        metric: str,
        value: float,
        baseline_mean: float,
        occurred_at: datetime,
        targets: list[str],
        today_context: dict[str, Any] | None = None,
        severity: str = "warning",
    ) -> EventRecordResult:
        occurred_at = _as_utc(occurred_at)
        normalized_value = float(value)
        normalized_mean = float(baseline_mean)
        if not isinstance(metric, str) or metric not in _SUPPORTED_METRICS:
            raise ValueError("unsupported event metric")
        if severity not in {"info", "warning", "critical"}:
            raise ValueError("unsupported event severity")
        if not math.isfinite(normalized_value) or not math.isfinite(normalized_mean):
            raise ValueError("event values must be finite")
        event_key = _make_event_key(metric, occurred_at, normalized_value)
        topic = _TOPICS.get(metric, "身体状态变化")
        context = {
            "metric": metric,
            "value": normalized_value,
            "baseline": {"mean": normalized_mean},
            "today": {},
        }
        safe_today = {}
        for key in _TODAY_CONTEXT_KEYS:
            if today_context and key in today_context:
                item = today_context[key]
                if item is not None and _is_safe_context_value(item):
                    safe_today[key] = item
        context["today"] = safe_today
        target_snapshot = list(
            dict.fromkeys(
                target for target in targets if isinstance(target, str) and target
            )
        )
        expires_at = occurred_at + timedelta(minutes=30)

        with self._api._lock, self._api._connection() as conn:
            existing = conn.execute(
                "SELECT id FROM alerts WHERE event_key = ?", (event_key,)
            ).fetchone()
            if existing is not None:
                return EventRecordResult(False, existing["id"], event_key)
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO alerts (
                        timestamp, metric, value, baseline_mean, event_key,
                        expires_at, targets_json, context_json, severity, topic
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        occurred_at.isoformat(),
                        metric,
                        normalized_value,
                        normalized_mean,
                        event_key,
                        expires_at.isoformat(),
                        json.dumps(target_snapshot, ensure_ascii=False),
                        json.dumps(
                            context,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ),
                        severity,
                        topic,
                    ),
                )
                return EventRecordResult(True, cursor.lastrowid, event_key)
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT id FROM alerts WHERE event_key = ?", (event_key,)
                ).fetchone()
                if existing is None:
                    raise
                return EventRecordResult(False, existing["id"], event_key)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_LOCAL_TIMEZONE)
    return value.astimezone(timezone.utc)


def _make_event_key(metric: str, occurred_at: datetime, value: float) -> str:
    payload = json.dumps(
        [metric, occurred_at.isoformat(), float(value)],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return "health:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_body_monitor_api() -> BodyMonitorExtensionAPI | None:
    with _active_api_lock:
        return _active_api


def register_body_monitor_api(api: BodyMonitorExtensionAPI) -> None:
    global _active_api
    with _active_api_lock:
        _active_api = api


def unregister_body_monitor_api(api: BodyMonitorExtensionAPI) -> None:
    global _active_api
    with _active_api_lock:
        if _active_api is api:
            _active_api = None
