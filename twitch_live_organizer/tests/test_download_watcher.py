import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from twitch_live_organizer.organizer_core import download_watcher
from twitch_live_organizer.organizer_core.file_organizer import FileOrganizer
from twitch_live_organizer.organizer_core.twitch_api import (
    PendingDownload,
    load_archive_state,
    save_archive_state,
)


class TwitchStub:
    def __init__(self, *_args):
        pass

    def user_id_by_login(self, _login):
        return "channel-id"

    def archived_videos(self, _user_id, known_video_id=None):
        return [{"id": known_video_id or "video-1"}]


class DownloadWatcherTests(unittest.TestCase):
    def test_pending_paths_must_be_mp4s_in_the_staging_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            staging_directory = root / "staging"
            staging_directory.mkdir()
            outside_path = root / "other.mp4"

            with self.assertRaises(RuntimeError):
                download_watcher._validated_download_path(
                    outside_path, staging_directory
                )

    def test_completed_output_hook_reports_final_mp4(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            staging_directory = Path(temporary_directory) / ".staging"
            staging_directory.mkdir()
            output = staging_directory / "video-1-Game.mp4"
            output.write_bytes(b"video")
            options_seen = {}

            class FakeYoutubeDL:
                def __init__(self, options):
                    options_seen.update(options)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def download(self, _urls):
                    for hook in options_seen["post_hooks"]:
                        hook(str(output))
                    return 0

            class FakeDownloadError(Exception):
                pass

            fake_yt_dlp = SimpleNamespace(
                YoutubeDL=FakeYoutubeDL,
                utils=SimpleNamespace(DownloadError=FakeDownloadError),
            )
            reported_paths = []
            resume_event = threading.Event()
            resume_event.set()
            with mock.patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}):
                result = download_watcher.download_archive_video(
                    {"id": "video-1", "title": "Game"},
                    threading.Event(),
                    resume_event,
                    staging_directory,
                    logging.getLogger("test-yt-dlp"),
                    lambda _video_id, path: reported_paths.append(path),
                )

            self.assertEqual(result, [output])
            self.assertEqual(reported_paths, [output])
            self.assertEqual(options_seen["paths"]["home"], str(staging_directory))
            self.assertEqual(
                options_seen["paths"]["temp"], str(staging_directory / ".tmp")
            )

    def test_failed_file_move_is_retried_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_root = root / "output"
            staging_directory = (
                output_root / download_watcher.DOWNLOAD_STAGING_DIRECTORY_NAME
            )
            staging_directory.mkdir(parents=True)
            source = staging_directory / "video-1-Game.mp4"
            source.write_bytes(b"video")
            legacy_folder = root / "old-watch-folder"
            legacy_folder.mkdir()
            unrelated = legacy_folder / "manual.mp4"
            unrelated.write_bytes(b"unrelated")
            state_path = root / "archive-state.json"
            save_archive_state(
                state_path,
                "video-1",
                [PendingDownload("video-1", source)],
            )
            logger = logging.getLogger("test-download-watcher")
            logger.addHandler(logging.NullHandler())

            class FailingOrganizer:
                def __init__(self, *_args):
                    pass

                def process_file(self, _path):
                    first_stop.set()
                    raise OSError("temporary move failure")

            class StopAfterProcessing(FileOrganizer):
                def process_file(self, path):
                    result = super().process_file(path)
                    second_stop.set()
                    return result

            def run_watcher(stop_event, organizer_class):
                resume_event = threading.Event()
                resume_event.set()
                with mock.patch.object(
                    download_watcher, "TwitchClient", TwitchStub
                ), mock.patch.object(
                    download_watcher, "FileOrganizer", organizer_class
                ):
                    download_watcher.watch(
                        stop_event,
                        resume_event,
                        client_id="client",
                        client_secret="secret",
                        channel_login="channel",
                        output_root=output_root,
                        category_to_directory={"Game": "Games"},
                        archive_state_path=state_path,
                        logger=logger,
                    )

            first_stop = threading.Event()
            run_watcher(first_stop, FailingOrganizer)
            self.assertTrue(source.exists())
            self.assertEqual(
                load_archive_state(state_path)[2],
                [PendingDownload("video-1", source)],
            )

            second_stop = threading.Event()
            run_watcher(second_stop, StopAfterProcessing)

            self.assertFalse(source.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue((output_root / "Games" / "Games #1.mp4").exists())
            self.assertEqual(load_archive_state(state_path)[2], [])


if __name__ == "__main__":
    unittest.main()
