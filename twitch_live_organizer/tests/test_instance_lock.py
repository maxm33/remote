import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from twitch_live_organizer.organizer_core.instance_lock import SingleInstanceLock


class SingleInstanceLockTests(unittest.TestCase):
    def test_second_process_cannot_acquire_existing_lock(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "organizer.lock"
            script = """
import sys
from twitch_live_organizer.organizer_core.instance_lock import (
    InstanceAlreadyRunningError,
    SingleInstanceLock,
)
try:
    SingleInstanceLock(sys.argv[1]).acquire()
except InstanceAlreadyRunningError:
    raise SystemExit(23)
raise SystemExit(0)
"""
            with SingleInstanceLock(lock_path):
                result = subprocess.run(
                    [sys.executable, "-c", script, str(lock_path)],
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                )

            self.assertEqual(result.returncode, 23, result.stderr)

    def test_released_lock_can_be_acquired_again(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "organizer.lock"
            with SingleInstanceLock(lock_path):
                pass

            with SingleInstanceLock(lock_path):
                self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
