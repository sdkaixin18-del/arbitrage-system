from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.kstr_runtime_log import (
    append_kstr_runtime_event,
    kstr_runtime_logs_overview,
)


class KstrRuntimeLogTest(unittest.TestCase):
    def test_runtime_events_are_persisted_and_returned_latest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            append_kstr_runtime_event(
                "page_served",
                stage="page",
                etf_code="588000",
                message="页面数据读取成功",
                duration_ms=12.34,
                details={"servedFrom": "snapshot", "itemCount": 120},
                path=path,
            )
            append_kstr_runtime_event(
                "background_refresh_failed",
                level="error",
                stage="market_data",
                status="failed",
                etf_code="588000",
                message="后台实时行情刷新失败",
                path=path,
            )

            overview = kstr_runtime_logs_overview(20, path=path)

            self.assertEqual(overview["count"], 2)
            self.assertEqual(overview["items"][0]["event"], "background_refresh_failed")
            self.assertEqual(overview["items"][1]["details"]["servedFrom"], "snapshot")
            self.assertEqual(overview["summary"]["lastHourErrors"], 1)
            self.assertEqual(overview["summary"]["lastError"], "后台实时行情刷新失败")

    def test_runtime_log_level_filter_and_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("not-json\n", encoding="utf-8")
            append_kstr_runtime_event(
                "page_served",
                stage="page",
                message="ok",
                path=path,
            )
            append_kstr_runtime_event(
                "page_failed",
                level="error",
                stage="page",
                status="failed",
                message="failed",
                path=path,
            )

            overview = kstr_runtime_logs_overview(20, level="error", path=path)

            self.assertEqual(overview["count"], 1)
            self.assertEqual(overview["items"][0]["event"], "page_failed")


if __name__ == "__main__":
    unittest.main()
