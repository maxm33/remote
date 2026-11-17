import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from twitch_live_organizer import twitch_live_organizer


class UpdateWatcherTests(unittest.TestCase):
    def test_checks_for_updates_before_waiting_for_hourly_interval(self):
        events = []
        run_creationflags = []
        repository_root = Path(tempfile.gettempdir())

        class StopAfterFirstWait:
            def is_set(self):
                return False

            def wait(self, timeout):
                events.append(("wait", timeout))
                return True

        def fake_run(arguments, **kwargs):
            events.append(("run", arguments))
            run_creationflags.append(kwargs.get("creationflags"))
            if arguments[-1] == "--show-toplevel":
                stdout = str(repository_root)
            elif arguments[-1] == "HEAD":
                stdout = "current-commit"
            else:
                stdout = ""
            return SimpleNamespace(
                stdout=stdout,
                stderr="",
                returncode=0,
            )

        with mock.patch.object(
            twitch_live_organizer.subprocess, "run", side_effect=fake_run
        ), mock.patch.object(twitch_live_organizer, "LOGGER", logging.getLogger("test")):
            twitch_live_organizer.watch_for_updates(StopAfterFirstWait())

        self.assertEqual(
            [event[0] for event in events],
            ["run", "run", "run", "run", "wait"],
        )
        self.assertEqual(
            events[-1], ("wait", twitch_live_organizer.UPDATE_CHECK_SECONDS)
        )
        self.assertEqual(
            run_creationflags,
            [getattr(twitch_live_organizer.subprocess, "CREATE_NO_WINDOW", 0)] * 4,
        )


if __name__ == "__main__":
    unittest.main()
