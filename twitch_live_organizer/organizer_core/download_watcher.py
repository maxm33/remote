import logging
import sys
import time
from pathlib import Path

from .file_organizer import FileOrganizer
from .logging_utils import ProgressFileHandler
from .twitch_api import (
    PendingDownload,
    TwitchClient,
    load_archive_state,
    save_archive_state,
)

POLL_SECONDS = 15
ARCHIVE_POLL_SECONDS = 60
DOWNLOAD_STAGING_DIRECTORY_NAME = ".twitch-live-organizer-downloads"


class YtDlpLogger:
    def __init__(self, logger):
        self.logger = logger

    def debug(self, message):
        self.logger.debug("%s", message)

    def warning(self, message):
        self.logger.warning("%s", message)

    def error(self, message):
        self.logger.error("%s", message)


def format_bytes(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return "{:.1f} {}".format(value, unit)
        value /= 1024


def log_download_progress(logger, message, complete=False):
    record = logger.makeRecord(
        logger.name, logging.INFO, __file__, 0, message, (), None
    )
    progress_handlers = [
        handler
        for handler in logger.handlers
        if isinstance(handler, ProgressFileHandler)
        and record.levelno >= handler.level
    ]
    if not progress_handlers:
        logger.info("%s", message)
        return

    for handler in progress_handlers:
        handler.write_progress(record, complete=complete)


def finish_download_progress(logger, video_id, message):
    log_download_progress(logger, "VOD {} {}".format(video_id, message), complete=True)


def release_download_progress(logger):
    for handler in logger.handlers:
        if isinstance(handler, ProgressFileHandler):
            handler.acquire()
            try:
                handler.progress_offset = None
            finally:
                handler.release()


def download_archive_video(
    video,
    stop_event,
    resume_event,
    download_directory,
    logger,
    on_download,
):
    try:
        import yt_dlp
    except ImportError as error:
        raise RuntimeError(
            "yt-dlp is missing. Run install.bat to install it."
        ) from error

    video_id = video["id"]
    video_url = "https://www.twitch.tv/videos/{}".format(video_id)
    last_progress_at = [0.0]
    terminal_progress = sys.stdout is not None and sys.stdout.isatty()
    last_progress_length = [0]
    downloaded_paths = []
    download_directory = Path(download_directory)
    download_directory.mkdir(parents=True, exist_ok=True)
    temporary_directory = download_directory / ".tmp"

    def pause_download(status):
        while not stop_event.is_set() and not resume_event.wait(timeout=0.5):
            pass
        if status.get("status") not in ("downloading", "finished"):
            return

        downloaded = status.get("downloaded_bytes") or 0
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        now = time.monotonic()
        if status.get("status") != "finished" and now - last_progress_at[0] < 1:
            return
        last_progress_at[0] = now

        if total:
            fraction = min(max(downloaded / total, 0), 1)
            filled = int(fraction * 20)
            bar = "#" * filled + "-" * (20 - filled)
            progress = "{:.1f}% {} / {}".format(
                fraction * 100, format_bytes(downloaded), format_bytes(total)
            )
        else:
            bar = "?" * 20
            progress = "{} / total unknown".format(format_bytes(downloaded))

        speed = status.get("speed")
        speed_text = " at {}/s".format(format_bytes(speed)) if speed else ""
        eta = status.get("eta")
        eta_text = ""
        if eta is not None:
            eta_text = " (ETA {:02d}:{:02d})".format(int(eta) // 60, int(eta) % 60)
        message = "VOD {} download: [{}] {}{}{}".format(
            video_id,
            bar,
            progress,
            speed_text,
            eta_text,
        )
        if terminal_progress:
            padding = " " * max(0, last_progress_length[0] - len(message))
            sys.stdout.write("\r" + message + padding)
            sys.stdout.flush()
            last_progress_length[0] = len(message)
        log_download_progress(logger, message)

    def record_download(path):
        downloaded_path = Path(path)
        if not downloaded_path.is_absolute():
            downloaded_path = download_directory / downloaded_path
        downloaded_path = downloaded_path.resolve()
        downloaded_paths.append(downloaded_path)
        if downloaded_path.suffix.lower() == ".mp4":
            on_download(video_id, downloaded_path)

    options = {
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "paths": {
            "home": str(download_directory),
            "temp": str(temporary_directory),
        },
        "outtmpl": "%(id)s-%(title)s.%(ext)s",
        "quiet": True,
        "logger": YtDlpLogger(logger),
        "progress_hooks": [pause_download],
        "post_hooks": [record_download],
    }
    logger.info("Downloading Twitch VOD %s: %s", video_id, video.get("title", ""))
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            result = downloader.download([video_url])
    except yt_dlp.utils.DownloadError as error:
        finish_download_progress(logger, video_id, "download failed.")
        raise RuntimeError(
            "Could not download Twitch VOD {}: {}".format(video_id, error)
        ) from error
    except Exception:
        finish_download_progress(logger, video_id, "download failed.")
        raise
    finally:
        if terminal_progress and last_progress_length[0]:
            sys.stdout.write("\n")
            sys.stdout.flush()
    if result:
        finish_download_progress(logger, video_id, "download failed.")
        raise RuntimeError(
            "yt-dlp failed to download Twitch VOD {} (exit code {}).".format(
                video_id, result
            )
        )
    release_download_progress(logger)
    logger.info("Finished downloading Twitch VOD %s.", video_id)
    return downloaded_paths


def _validated_download_path(path, download_directory):
    path = Path(path)
    if not path.is_absolute():
        path = Path(download_directory) / path
    path = path.resolve()
    if (
        path.suffix.lower() != ".mp4"
        or path.parent != Path(download_directory).resolve()
    ):
        raise RuntimeError(
            "Pending download is not an MP4 in the download staging directory: {}."
            .format(path)
        )
    return path


def watch(
    stop_event,
    resume_event,
    *,
    client_id,
    client_secret,
    channel_login,
    output_root,
    category_to_directory,
    archive_state_path,
    logger,
):
    twitch = TwitchClient(client_id, client_secret)
    state_path = Path(archive_state_path)
    download_directory = (
        Path(output_root) / DOWNLOAD_STAGING_DIRECTORY_NAME
    ).resolve()
    download_directory.mkdir(parents=True, exist_ok=True)
    initialized, last_video_id, pending_downloads = load_archive_state(state_path)
    pending_downloads = [
        PendingDownload(
            pending.video_id,
            _validated_download_path(pending.path, download_directory),
        )
        for pending in pending_downloads
    ]

    file_organizer = FileOrganizer(output_root, category_to_directory, logger)
    user_id = None
    next_archive_check = 0
    next_file_check = 0

    def persist_state():
        save_archive_state(state_path, last_video_id, pending_downloads)

    def record_download(video_id, path):
        nonlocal last_video_id
        path = _validated_download_path(path, download_directory)
        pending = PendingDownload(video_id, path)
        if pending not in pending_downloads:
            pending_downloads.append(pending)
        last_video_id = video_id
        persist_state()

    while not stop_event.is_set():
        if not resume_event.wait(timeout=0.5):
            continue
        now = time.monotonic()
        if now >= next_file_check:
            for pending in pending_downloads[:]:
                if not pending.path.is_file():
                    logger.warning(
                        "Pending yt-dlp output %s is missing; removing it from the "
                        "retry queue.",
                        pending.path,
                    )
                    pending_downloads.remove(pending)
                    persist_state()
                    continue
                try:
                    if file_organizer.process_file(pending.path):
                        pending_downloads.remove(pending)
                        persist_state()
                except Exception as error:
                    logger.error(
                        "Could not process %s; will retry: %s", pending.path, error
                    )
            next_file_check = now + POLL_SECONDS

        if stop_event.is_set():
            break

        if now >= next_archive_check:
            next_archive_check = now + ARCHIVE_POLL_SECONDS
            try:
                if user_id is None:
                    user_id = twitch.user_id_by_login(channel_login)
                    if user_id is None:
                        raise RuntimeError(
                            "Twitch user {!r} was not found.".format(channel_login)
                        )
                    logger.info("Watching Twitch archive for %s.", channel_login)

                videos = twitch.archived_videos(
                    user_id,
                    known_video_id=last_video_id if initialized else None,
                )
                video_to_download = None
                if not initialized:
                    last_video_id = videos[0]["id"] if videos else None
                    persist_state()
                    initialized = True
                    if last_video_id is None:
                        logger.info(
                            "No existing VOD found for %s; waiting for its first archive.",
                            channel_login,
                        )
                    else:
                        logger.info(
                            "Using existing VOD %s as the initial archive marker.",
                            last_video_id,
                        )
                elif last_video_id is None:
                    video_to_download = videos[0] if videos else None
                else:
                    marker_index = next(
                        (
                            index
                            for index, video in enumerate(videos)
                            if video["id"] == last_video_id
                        ),
                        None,
                    )
                    if marker_index is None:
                        missing_video_id = last_video_id
                        last_video_id = videos[0]["id"] if videos else None
                        persist_state()
                        if last_video_id is None:
                            logger.warning(
                                "Saved archive marker %s was not found for %s and "
                                "the channel has no VODs; waiting for its next archive.",
                                missing_video_id,
                                channel_login,
                            )
                        else:
                            logger.warning(
                                "Saved archive marker %s was not found for %s; using "
                                "the latest VOD %s as the new marker without "
                                "downloading older VODs.",
                                missing_video_id,
                                channel_login,
                                last_video_id,
                            )
                    else:
                        video_to_download = (
                            videos[marker_index - 1] if marker_index > 0 else None
                        )

                if (
                    initialized
                    and not stop_event.is_set()
                    and resume_event.is_set()
                    and video_to_download is not None
                ):
                    downloaded_paths = download_archive_video(
                        video_to_download,
                        stop_event,
                        resume_event,
                        download_directory,
                        logger,
                        record_download,
                    )
                    if not any(
                        path.suffix.lower() == ".mp4" for path in downloaded_paths
                    ):
                        logger.warning(
                            "yt-dlp completed VOD %s without reporting an MP4 output; "
                            "the archive marker was not advanced.",
                            video_to_download["id"],
                        )
            except Exception as error:
                logger.error("Archive check or download failed; will retry: %s", error)
        stop_event.wait(POLL_SECONDS)
