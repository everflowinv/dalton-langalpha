"""Regression checks for migrated trajectory usage; no provider or message calls."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import usage_sources as usage


class SqliteUsageTests(unittest.TestCase):
    def test_migrated_legacy_duplicate_and_native_openai_inclusion(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        event = {"type": "model.completed", "traceId": "trace", "runId": "run",
                 "seq": 3, "ts": now, "provider": "xai", "modelId": "grok-4.3",
                 "data": {"usage": {"input": 10, "cacheRead": 20, "output": 5, "total": 35}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "agent.sqlite"
            connection = sqlite3.connect(db)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE trajectory_runtime_events (event_json TEXT)")
            excluded = {**event, "traceId": "openai", "provider": "openai"}
            old = {**event, "traceId": "old", "ts": "2000-01-01T00:00:00Z"}
            for record in [event, excluded, old]:
                connection.execute("INSERT INTO trajectory_runtime_events VALUES (?)", (json.dumps(record),))
            connection.commit()
            legacy = root / "legacy.jsonl"
            legacy.write_text(json.dumps(event) + "\n")
            with patch.object(usage, "ROOTS", {**usage.ROOTS, "trajectory-db": [str(db)], "trajectory-file": [str(legacy)]}):
                end=dt.datetime.now(dt.timezone.utc).timestamp()
                aggregate = usage.Evidence(end-7*86400,end)
                usage.scan_trajectories(aggregate)
                self.assertEqual(len(aggregate.records),2)
                self.assertEqual({r['provider'] for r in aggregate.records.values()},{'openai','xai'})
                self.assertEqual(sum(sum(r['v']) for r in aggregate.records.values()),70)
            connection.close()

    def test_broken_sqlite_is_not_silently_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "broken.sqlite"
            db.write_text("invalid database")
            with patch.object(usage, "ROOTS", {**usage.ROOTS, "trajectory-db": [str(db)], "trajectory-file": []}):
                with self.assertRaises(sqlite3.DatabaseError):
                    usage.scan_trajectories(usage.Evidence(0,1e12))


if __name__ == "__main__":
    unittest.main()
