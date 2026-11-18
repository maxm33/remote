import errno
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from twitch_live_organizer.organizer_core.file_organizer import (
    FileOrganizer,
    move_without_overwriting,
)


class FileOrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.output_root = self.root / "output"
        self.destination_directory = self.output_root / "Games"
        self.destination_directory.mkdir(parents=True)
        self.organizer = FileOrganizer(
            self.output_root, {"Game": "Games"}, logging.getLogger("test-organizer")
        )

    def make_source(self, name):
        path = self.root / name
        path.write_bytes(b"video")
        return path

    def test_caches_numbering_for_unchanged_directory(self):
        scan = self.organizer._scan_max_used_id
        with mock.patch.object(
            self.organizer, "_scan_max_used_id", wraps=scan
        ) as scan_mock:
            first = self.make_source("video-1-Game.mp4")
            second = self.make_source("video-2-Game.mp4")
            self.organizer.process_file(first)
            self.organizer.process_file(second)

        self.assertEqual(scan_mock.call_count, 1)
        self.assertTrue((self.destination_directory / "Games #1.mp4").exists())
        self.assertTrue((self.destination_directory / "Games #2.mp4").exists())

    def test_rescans_when_directory_changes_externally(self):
        first = self.make_source("video-1-Game.mp4")
        self.organizer.process_file(first)

        external = self.destination_directory / "Games #100.mp4"
        external.write_bytes(b"external")
        stat = self.destination_directory.stat()
        os.utime(
            self.destination_directory,
            ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000),
        )

        second = self.make_source("video-2-Game.mp4")
        self.organizer.process_file(second)

        self.assertTrue((self.destination_directory / "Games #101.mp4").exists())

    def test_processes_download_named_after_destination_directory(self):
        organizer = FileOrganizer(
            self.output_root,
            {"Indiemaking": "Indie Making"},
            logging.getLogger("test-organizer-directory-title"),
        )
        source = self.make_source("12345-Indie Making.mp4")

        self.assertTrue(organizer.process_file(source))
        self.assertTrue(
            (self.output_root / "Indie Making" / "Indie Making #1.mp4").exists()
        )

    def test_cross_device_collision_does_not_delete_existing_destination(self):
        source = self.make_source("source.mp4")
        destination = self.root / "destination.mp4"
        destination.write_bytes(b"existing")

        with mock.patch(
            "twitch_live_organizer.organizer_core.file_organizer.os.rename",
            side_effect=OSError(errno.EXDEV, "cross-device move"),
        ):
            with self.assertRaises(FileExistsError):
                move_without_overwriting(source, destination)

        self.assertEqual(destination.read_bytes(), b"existing")
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
