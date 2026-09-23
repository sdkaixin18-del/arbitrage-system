from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import kstr_quote_archive


class KstrQuoteArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        kstr_quote_archive._last_bucket_by_code.clear()

    @staticmethod
    def payload(timestamp: datetime) -> dict:
        return {
            "updatedAt": timestamp,
            "aMarketPhase": "continuous",
            "quoteFreshness": {
                "thresholdSeconds": 45,
                "aEtfAgeSeconds": 2,
                "kstrAgeSeconds": 1,
            },
            "aEtf": {"bid": 1.88, "ask": 1.881},
            "kstr": {"bid": 25.8, "ask": 25.82},
            "fx": {"usdCny": 6.77},
            "hedgeBeta": 1.01,
            "structuralModel": {"historyEnd": "2026-07-22"},
        }

    def test_archive_deduplicates_each_ten_second_bucket(self) -> None:
        timestamp = datetime(2026, 7, 23, 5, 0, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.jsonl"
            self.assertTrue(
                kstr_quote_archive.archive_kstr_quote_payload(
                    self.payload(timestamp), "588000", path=path
                )
            )
            self.assertFalse(
                kstr_quote_archive.archive_kstr_quote_payload(
                    self.payload(timestamp + timedelta(seconds=8)),
                    "588000.SH",
                    path=path,
                )
            )
            self.assertTrue(
                kstr_quote_archive.archive_kstr_quote_payload(
                    self.payload(timestamp + timedelta(seconds=10)),
                    "588000",
                    path=path,
                )
            )
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]["aQuoteFresh"])
            self.assertTrue(rows[0]["kstrQuoteFresh"])
            self.assertEqual(
                rows[0]["dynamicBetaAsOf"], "2026-07-22T00:00:00+00:00"
            )

    def test_archive_ignores_non_default_etf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.jsonl"
            self.assertFalse(
                kstr_quote_archive.archive_kstr_quote_payload(
                    self.payload(datetime.now(timezone.utc)),
                    "588050",
                    path=path,
                )
            )
            self.assertFalse(path.exists())

    def test_archive_ignores_bybit_to_keep_legacy_walkforward_series_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.jsonl"
            payload = self.payload(datetime.now(timezone.utc))
            payload["kstr"]["exchange"] = "by"
            self.assertFalse(
                kstr_quote_archive.archive_kstr_quote_payload(
                    payload,
                    "588000",
                    path=path,
                )
            )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
