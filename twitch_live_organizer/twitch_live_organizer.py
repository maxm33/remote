import argparse
import errno
import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")

try:
    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        CONFIG = json.load(config_file)
except FileNotFoundError as error:
    raise RuntimeError("Configuration file not found: {}".format(CONFIG_PATH)) from error
except json.JSONDecodeError as error:
    raise RuntimeError(
        "Invalid JSON in {} at line {}, column {}.".format(
            CONFIG_PATH, error.lineno, error.colno
        )
    ) from error

if not isinstance(CONFIG, dict):
    raise RuntimeError("{} must contain a JSON object.".format(CONFIG_PATH))


def config_string(name):
    if name not in CONFIG:
        raise RuntimeError("Missing '{}' in {}.".format(name, CONFIG_PATH))
    value = CONFIG[name]
    if not isinstance(value, str):
        raise RuntimeError("'{}' in {} must be a string.".format(name, CONFIG_PATH))
    return value


def configure_twitch_credentials():
    global CLIENT_ID, CLIENT_SECRET

    if CLIENT_ID and CLIENT_SECRET:
        print("Twitch API credentials are already configured.")
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Twitch API credentials are missing. Run install.bat or start this "
            "script from a terminal to enter them."
        )

    print("Enter your Twitch application credentials.")
    client_id = input("Twitch Client ID: ").strip()
    while not client_id:
        print("Client ID cannot be empty.")
        client_id = input("Twitch Client ID: ").strip()
    client_secret = getpass.getpass("Twitch Client Secret: ").strip()
    while not client_secret:
        print("Client Secret cannot be empty.")
        client_secret = getpass.getpass("Twitch Client Secret: ").strip()

    updated_config = dict(CONFIG)
    updated_config["twitch_client_id"] = client_id
    updated_config["twitch_client_secret"] = client_secret
    temporary_path = CONFIG_PATH.with_suffix(".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as config_file:
        json.dump(updated_config, config_file, indent=2, ensure_ascii=False)
        config_file.write("\n")
    temporary_path.replace(CONFIG_PATH)

    CONFIG.update(updated_config)
    CLIENT_ID = client_id
    CLIENT_SECRET = client_secret
    print("Twitch API credentials saved to {}.".format(CONFIG_PATH))


WATCH_FOLDER_VALUE = config_string("watch_folder")
OUTPUT_ROOT_VALUE = config_string("output_root")
if not WATCH_FOLDER_VALUE.strip():
    raise RuntimeError("'watch_folder' in {} cannot be empty.".format(CONFIG_PATH))
if not OUTPUT_ROOT_VALUE.strip():
    raise RuntimeError("'output_root' in {} cannot be empty.".format(CONFIG_PATH))

WATCH_FOLDER = Path(WATCH_FOLDER_VALUE)
OUTPUT_ROOT = Path(OUTPUT_ROOT_VALUE)
CLIENT_ID = config_string("twitch_client_id")
CLIENT_SECRET = config_string("twitch_client_secret")
CHANNEL_LOGIN = config_string("twitch_channel_login")

POLL_SECONDS = 5
STABLE_SECONDS = 15
ARCHIVE_POLL_SECONDS = 60

if "category_to_directory" not in CONFIG:
    raise RuntimeError(
        "Missing 'category_to_directory' in {}.".format(CONFIG_PATH)
    )
CATEGORY_TO_DIRECTORY = CONFIG["category_to_directory"]

if not isinstance(CATEGORY_TO_DIRECTORY, dict) or not all(
    isinstance(category, str) and isinstance(directory, str)
    for category, directory in CATEGORY_TO_DIRECTORY.items()
):
    raise RuntimeError(
        "'category_to_directory' in {} must map strings to strings.".format(CONFIG_PATH)
    )

API_BASE = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STARTUP_FILE_NAME = "TwitchLiveOrganizer.cmd"
DOWNLOAD_STAGING_DIRECTORY_NAME = ".twitch-live-organizer-downloads"
LOGGER = logging.getLogger("twitch_live_organizer")
LOG_FILE_NAME = "organizer.log"


class TwitchClient:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = None
        self.token_expires_at = 0

    def _get_app_token(self):
        form = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            }
        ).encode("ascii")
        request = urllib.request.Request(
            TOKEN_URL,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.access_token = data["access_token"]
        self.token_expires_at = time.time() + int(data["expires_in"]) - 60

    def _get_json(self, url, retry=True):
        if self.access_token is None or time.time() >= self.token_expires_at:
            self._get_app_token()
        request = urllib.request.Request(
            url,
            headers={
                "Client-ID": self.client_id,
                "Authorization": "Bearer " + self.access_token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 401 and retry:
                self.access_token = None
                return self._get_json(url, retry=False)
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                "Twitch API returned HTTP {}: {}".format(error.code, detail)
            ) from error

    def user_id_by_login(self, login):
        params = urllib.parse.urlencode({"login": login})
        users = self._get_json("{}/users?{}".format(API_BASE, params))["data"]
        return users[0]["id"] if users else None

    def archived_videos(self, user_id, known_video_id=None):
        videos = []
        cursor = None
        while True:
            params = {
                "user_id": user_id,
                "type": "archive",
                "sort": "time",
                "first": 100,
            }
            if cursor:
                params["after"] = cursor
            page = self._get_json(
                "{}/videos?{}".format(API_BASE, urllib.parse.urlencode(params))
            )
            page_videos = page["data"]
            videos.extend(page_videos)
            if known_video_id and any(
                video["id"] == known_video_id for video in page_videos
            ):
                break
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor or not known_video_id:
                break
        return videos


def startup_folder():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; cannot locate the Startup folder")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def python_windowless_executable():
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    return windowless if windowless.exists() else executable


def install_startup():
    folder = startup_folder()
    folder.mkdir(parents=True, exist_ok=True)
    launcher = folder / STARTUP_FILE_NAME
    command = '"{}" "{}"'.format(python_windowless_executable(), Path(__file__).resolve())
    launcher.write_text("@echo off\r\nstart \"\" {}\r\n".format(command), encoding="utf-8")
    print("Installed startup launcher: {}".format(launcher))


def uninstall_startup():
    launcher = startup_folder() / STARTUP_FILE_NAME
    if launcher.exists():
        launcher.unlink()
        print("Removed startup launcher: {}".format(launcher))
    else:
        print("Startup launcher was not installed.")


def app_data_directory():
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        log_directory = Path(local_appdata) / "TwitchLiveOrganizer" / "logs"
    else:
        log_directory = Path.home() / "AppData" / "Local" / "TwitchLiveOrganizer" / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    return log_directory


class ProgressFileHandler(logging.FileHandler):
    PROGRESS_LINE_WIDTH = 256

    def __init__(self, filename, encoding="utf-8"):
        super().__init__(filename, encoding=encoding)
        self.progress_offset = None

    def write_progress(self, record, complete=False):
        self.acquire()
        try:
            self.flush()
            message = self.format(record).encode(self.encoding or "utf-8")
            if len(message) > self.PROGRESS_LINE_WIDTH:
                raise RuntimeError(
                    "Download progress log entry exceeds its fixed width."
                )

            line = message + b" " * (self.PROGRESS_LINE_WIDTH - len(message))
            newline = os.linesep.encode("ascii")
            with open(self.baseFilename, "r+b") as log_file:
                if self.progress_offset is None:
                    log_file.seek(0, os.SEEK_END)
                    self.progress_offset = log_file.tell()
                log_file.seek(self.progress_offset)
                log_file.write(line + newline)
                log_file.flush()
            if complete:
                self.progress_offset = None
        finally:
            self.release()


def configure_logging():
    log_file = app_data_directory() / LOG_FILE_NAME
    handler = ProgressFileHandler(log_file, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    return log_file


def open_log(log_file):
    escaped_path = str(log_file).replace("'", "''")
    command = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$logPath = '{}'; $lastWrite = [datetime]::MinValue; "
        "while ($true) {{ "
        "$writeTime = [System.IO.File]::GetLastWriteTimeUtc($logPath); "
        "if ($writeTime -ne $lastWrite) {{ "
        "Clear-Host; "
        "Get-Content -LiteralPath $logPath -Tail 100 -Encoding UTF8 | "
        "ForEach-Object {{ Write-Host $_.TrimEnd() }}; "
        "$lastWrite = $writeTime "
        "}}; Start-Sleep -Milliseconds 250 }}"
    ).format(escaped_path)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NoExit", "-Command", command],
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


def archive_state_path():
    return app_data_directory() / "archive-state.json"


def load_archive_state(state_path):
    if not state_path.exists():
        return False, None
    with state_path.open("r", encoding="utf-8") as state_file:
        state = json.load(state_file)
    if (
        not isinstance(state, dict)
        or state.get("initialized") is not True
        or (
            state.get("last_video_id") is not None
            and not isinstance(state.get("last_video_id"), str)
        )
    ):
        raise RuntimeError("Invalid Twitch archive state in {}.".format(state_path))
    return True, state["last_video_id"]


def save_archive_state(state_path, video_id):
    temporary_path = state_path.with_suffix(".tmp")
    with temporary_path.open("w", encoding="utf-8") as state_file:
        json.dump({"initialized": True, "last_video_id": video_id}, state_file)
    temporary_path.replace(state_path)


class YtDlpLogger:
    def debug(self, message):
        LOGGER.debug("%s", message)

    def warning(self, message):
        LOGGER.warning("%s", message)

    def error(self, message):
        LOGGER.error("%s", message)


def format_bytes(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return "{:.1f} {}".format(value, unit)
        value /= 1024


def log_download_progress(message, terminal_progress, complete=False):
    record = LOGGER.makeRecord(
        LOGGER.name, logging.INFO, __file__, 0, message, (), None
    )
    progress_handlers = [
        handler
        for handler in LOGGER.handlers
        if isinstance(handler, ProgressFileHandler)
        and record.levelno >= handler.level
    ]
    if not progress_handlers:
        LOGGER.info("%s", message)
        return

    for handler in progress_handlers:
        handler.write_progress(record, complete=complete)


def finish_download_progress(video_id, message, terminal_progress):
    log_download_progress(
        "VOD {} {}".format(video_id, message),
        terminal_progress,
        complete=True,
    )


def release_download_progress():
    for handler in LOGGER.handlers:
        if isinstance(handler, ProgressFileHandler):
            handler.acquire()
            try:
                handler.progress_offset = None
            finally:
                handler.release()


def download_archive_video(video, stop_event, resume_event):
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
        log_download_progress(message, terminal_progress)

    options = {
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        # Download and post-process (the ffmpeg fixup) inside a staging subfolder.
        # yt-dlp moves the finished file into the watch folder only after every
        # step is done, so the folder watcher never sees a file still in use.
        # The staging folder is on the same drive, so that final move is instant.
        "paths": {
            "home": str(WATCH_FOLDER),
            "temp": str(WATCH_FOLDER / DOWNLOAD_STAGING_DIRECTORY_NAME),
        },
        # Must stay relative: an absolute template would bypass "paths".
        "outtmpl": "%(id)s-%(title)s.%(ext)s",
        "quiet": True,
        "logger": YtDlpLogger(),
        "progress_hooks": [pause_download],
    }
    LOGGER.info("Downloading Twitch VOD %s: %s", video_id, video.get("title", ""))
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            result = downloader.download([video_url])
    except yt_dlp.utils.DownloadError as error:
        finish_download_progress(video_id, "download failed.", terminal_progress)
        raise RuntimeError(
            "Could not download Twitch VOD {}: {}".format(video_id, error)
        ) from error
    except Exception:
        finish_download_progress(video_id, "download failed.", terminal_progress)
        raise
    finally:
        if terminal_progress and last_progress_length[0]:
            sys.stdout.write("\n")
            sys.stdout.flush()
    if result:
        finish_download_progress(video_id, "download failed.", terminal_progress)
        raise RuntimeError(
            "yt-dlp failed to download Twitch VOD {} (exit code {}).".format(
                video_id, result
            )
        )
    release_download_progress()
    LOGGER.info("Finished downloading Twitch VOD %s.", video_id)


def watch_archives(stop_event, resume_event):
    twitch = TwitchClient(CLIENT_ID, CLIENT_SECRET)
    state_path = archive_state_path()
    initialized, last_video_id = load_archive_state(state_path)
    user_id = None

    while not stop_event.is_set():
        if not resume_event.wait(timeout=0.5):
            continue
        try:
            if user_id is None:
                user_id = twitch.user_id_by_login(CHANNEL_LOGIN)
                if user_id is None:
                    raise RuntimeError(
                        "Twitch user {!r} was not found.".format(CHANNEL_LOGIN)
                    )
                LOGGER.info("Watching Twitch archive for %s.", CHANNEL_LOGIN)

            videos = twitch.archived_videos(
                user_id,
                known_video_id=last_video_id if initialized else None,
            )
            video_to_download = None
            if not initialized:
                last_video_id = videos[0]["id"] if videos else None
                save_archive_state(state_path, last_video_id)
                initialized = True
                if last_video_id is None:
                    LOGGER.info(
                        "No existing VOD found for %s; waiting for its first archive.",
                        CHANNEL_LOGIN,
                    )
                else:
                    LOGGER.info(
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
                    save_archive_state(state_path, last_video_id)
                    if last_video_id is None:
                        LOGGER.warning(
                            "Saved archive marker %s was not found for %s and the "
                            "channel has no VODs; waiting for its next archive.",
                            missing_video_id,
                            CHANNEL_LOGIN,
                        )
                    else:
                        LOGGER.warning(
                            "Saved archive marker %s was not found for %s; using "
                            "the latest VOD %s as the new marker without "
                            "downloading older VODs.",
                            missing_video_id,
                            CHANNEL_LOGIN,
                            last_video_id,
                        )
                    video_to_download = None
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
                download_archive_video(video_to_download, stop_event, resume_event)
                last_video_id = video_to_download["id"]
                save_archive_state(state_path, last_video_id)
        except Exception as error:
            LOGGER.error("Archive check or download failed; will retry: %s", error)
        stop_event.wait(ARCHIVE_POLL_SECONDS)


def run_tray(log_file):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError(
            "Tray dependencies are missing. Run install.bat to install them."
        ) from error

    stop_event = threading.Event()
    resume_event = threading.Event()
    resume_event.set()

    image = Image.new("RGB", (64, 64), (35, 35, 35))
    draw = ImageDraw.Draw(image)
    draw.ellipse((9, 9, 55, 55), fill=(145, 70, 255))
    draw.text((21, 17), "T", fill=(255, 255, 255))

    def pause_text(_item):
        return "Pause" if resume_event.is_set() else "Resume"

    def toggle_pause(icon, _item):
        if resume_event.is_set():
            resume_event.clear()
            LOGGER.info("Paused by user.")
        else:
            resume_event.set()
            LOGGER.info("Resumed by user.")
        icon.update_menu()

    def show_log(_icon, _item):
        try:
            open_log(log_file)
        except OSError as error:
            LOGGER.error("Could not open the log window: %s", error)

    def exit_app(icon, _item):
        LOGGER.info("Stopping by user request.")
        stop_event.set()
        resume_event.set()
        icon.stop()

    icon = pystray.Icon(
        "twitch_live_organizer",
        image,
        "Twitch Live Organizer",
        menu=pystray.Menu(
            pystray.MenuItem("Open log", show_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(pause_text, toggle_pause),
            pystray.MenuItem("Exit", exit_app),
        ),
    )

    def run_watcher():
        try:
            watch(stop_event, resume_event)
        except Exception as error:
            LOGGER.error("Watcher stopped unexpectedly: %s", error)
            stop_event.set()
            icon.stop()

    def run_archive_watcher():
        try:
            watch_archives(stop_event, resume_event)
        except Exception as error:
            LOGGER.error("Archive watcher stopped unexpectedly: %s", error)
            stop_event.set()
            icon.stop()

    watcher = threading.Thread(
        target=run_watcher, name="twitch-live-organizer", daemon=True
    )
    archive_watcher = threading.Thread(
        target=run_archive_watcher, name="twitch-archive-watcher", daemon=True
    )
    watcher.start()
    archive_watcher.start()
    try:
        icon.run()
    finally:
        stop_event.set()
        resume_event.set()


def validate_configuration():
    if not WATCH_FOLDER.is_dir():
        raise RuntimeError("Watch folder does not exist: {}".format(WATCH_FOLDER))
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "Set 'twitch_client_id' and 'twitch_client_secret' in {}.".format(
                CONFIG_PATH
            )
        )
    if not CHANNEL_LOGIN.strip():
        raise RuntimeError(
            "'twitch_channel_login' in {} cannot be empty.".format(CONFIG_PATH)
        )
    if not CATEGORY_TO_DIRECTORY:
        raise RuntimeError(
            "Add at least one category mapping to 'category_to_directory' in {}."
            .format(CONFIG_PATH)
        )
    for directory_name in CATEGORY_TO_DIRECTORY.values():
        relative_directory = Path(directory_name)
        if (
            not directory_name
            or relative_directory.is_absolute()
            or relative_directory.drive
            or relative_directory.root
            or ".." in relative_directory.parts
            or not relative_directory.name
        ):
            raise RuntimeError(
                "Category directory paths must be relative to output_root and "
                "must not escape it: {!r}".format(directory_name)
            )


def next_destination(directory, directory_name):
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", directory_name).rstrip(" .")
    if not safe_name:
        raise RuntimeError("Directory name cannot produce a valid file name.")
    pattern = re.compile(
        r"^{} #(\d+)\.mp4$".format(re.escape(safe_name)),
        re.IGNORECASE,
    )
    used_ids = set()
    for existing in directory.iterdir():
        match = pattern.match(existing.name)
        if match:
            used_ids.add(int(match.group(1)))
    next_id = max(used_ids, default=0) + 1
    return directory / "{} #{}.mp4".format(safe_name, next_id)


def move_without_overwriting(source, destination):
    try:
        os.rename(source, destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        try:
            with source.open("rb") as source_file, destination.open("xb") as target_file:
                shutil.copyfileobj(source_file, target_file)
            shutil.copystat(source, destination)
            source.unlink()
        except OSError:
            if destination.exists():
                destination.unlink()
            raise


def category_from_title(title):
    if not isinstance(title, str):
        return None
    title_folded = title.casefold()
    matches = (
        category
        for category in CATEGORY_TO_DIRECTORY
        if category.casefold() in title_folded
    )
    return max(matches, key=len, default=None)


def process_file(path):
    filename_stem = path.stem
    _, separator, title = filename_stem.partition("-")
    if not separator:
        title = filename_stem
    if not title.strip():
        LOGGER.warning(
            "Could not read a title from the downloaded filename; leaving %s in place.",
            path,
        )
        return False

    category = category_from_title(title)
    if category is None:
        destination_directory = OUTPUT_ROOT
        destination_name = title
        LOGGER.info(
            "No configured category matched title %r; using it as the filename "
            "in %s.",
            title,
            OUTPUT_ROOT,
        )
    else:
        relative_directory = Path(CATEGORY_TO_DIRECTORY[category])
        destination_name = relative_directory.name
        destination_directory = OUTPUT_ROOT / relative_directory

    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = next_destination(destination_directory, destination_name)
    move_without_overwriting(path, destination)
    if category is None:
        LOGGER.info("Moved %s to %s (no category matched).", path, destination)
    else:
        LOGGER.info("Moved %s to %s (category: %s)", path, destination, category)
    return True


def watch(stop_event=None, resume_event=None):
    validate_configuration()
    if stop_event is None:
        stop_event = threading.Event()
    if resume_event is None:
        resume_event = threading.Event()
        resume_event.set()
    observed = {}
    LOGGER.info("Watching %s for MP4 files.", WATCH_FOLDER)

    while not stop_event.is_set():
        if not resume_event.wait(timeout=0.5):
            continue
        now = time.monotonic()
        present = set()
        for path in WATCH_FOLDER.iterdir():
            if stop_event.is_set():
                break
            resume_event.wait()
            if stop_event.is_set():
                break
            if not path.is_file() or path.suffix.lower() != ".mp4":
                continue
            present.add(path)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            previous = observed.get(path)
            if previous is None or previous[0] != size:
                observed[path] = (size, now)
                continue
            if now - previous[1] < STABLE_SECONDS:
                continue

            try:
                if process_file(path):
                    observed.pop(path, None)
                else:
                    observed[path] = (size, now)
            except Exception as error:
                LOGGER.error("Could not process %s; will retry: %s", path, error)
                observed[path] = (size, now)
        if stop_event.is_set():
            break
        for path in set(observed) - present:
            observed.pop(path, None)
        stop_event.wait(POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description="Sort lives using Twitch stream categories.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--install-startup",
        action="store_true",
        help="Start this script automatically when your Windows account logs in.",
    )
    actions.add_argument(
        "--uninstall-startup",
        action="store_true",
        help="Remove this script's Windows startup launcher.",
    )
    actions.add_argument(
        "--configure-credentials",
        action="store_true",
        help="Prompt for Twitch API credentials and save them in config.json.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.install_startup:
        install_startup()
    elif args.uninstall_startup:
        uninstall_startup()
    elif args.configure_credentials:
        configure_twitch_credentials()
    else:
        configure_twitch_credentials()
        log_file = configure_logging()
        LOGGER.info("Starting Twitch Live Organizer.")
        try:
            validate_configuration()
        except Exception as error:
            LOGGER.error("Invalid configuration; watcher not started: %s", error)
            raise SystemExit(1)
        if os.name == "nt":
            run_tray(log_file)
        else:
            LOGGER.info("Tray controls are available on Windows; running in foreground.")
            stop_event = threading.Event()
            resume_event = threading.Event()
            resume_event.set()
            archive_watcher = threading.Thread(
                target=watch_archives,
                args=(stop_event, resume_event),
                name="twitch-archive-watcher",
                daemon=True,
            )
            archive_watcher.start()
            try:
                watch(stop_event, resume_event)
            finally:
                stop_event.set()


if __name__ == "__main__":
    main()
