import json
import tempfile
import unittest
from pathlib import Path

from twitch_live_organizer.organizer_core.twitch_api import (
    PendingDownload,
    load_archive_state,
    save_archive_state,
)


class ArchiveStateTests(unittest.TestCase):
    def test_reads_legacy_state_without_pending_downloads(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "archive-state.json"
            state_path.write_text(
                json.dumps({"initialized": True, "last_video_id": "video-1"}),
                encoding="utf-8",
            )

            self.assertEqual(load_archive_state(state_path), (True, "video-1", []))

    def test_round_trips_pending_downloads(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "archive-state.json"
            pending = PendingDownload(
                "video-2", Path(temporary_directory) / "video-2-title.mp4"
            )

            save_archive_state(state_path, "video-2", [pending])

            initialized, last_video_id, pending_downloads = load_archive_state(
                state_path
            )
            self.assertTrue(initialized)
            self.assertEqual(last_video_id, "video-2")
            self.assertEqual(pending_downloads, [pending])


if __name__ == "__main__":
    unittest.main()
