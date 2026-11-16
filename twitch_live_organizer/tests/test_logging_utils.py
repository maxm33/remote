import logging
import tempfile
import unittest
from pathlib import Path

from twitch_live_organizer.organizer_core.logging_utils import ProgressFileHandler


class ProgressFileHandlerTests(unittest.TestCase):
    def test_clear_log_removes_progress_and_resets_progress_offset(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "organizer.log"
            handler = ProgressFileHandler(log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger("test-progress-file-handler")
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                record = logger.makeRecord(
                    logger.name, logging.INFO, __file__, 0, "Downloading", (), None
                )
                handler.write_progress(record)
                self.assertIsNotNone(handler.progress_offset)
                self.assertTrue(log_path.read_bytes())

                handler.clear_log()

                self.assertEqual(log_path.read_bytes(), b"")
                self.assertIsNone(handler.progress_offset)

                logger.info("After clear")
                handler.flush()
                self.assertEqual(
                    log_path.read_text(encoding="utf-8"), "After clear\n"
                )
            finally:
                logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
