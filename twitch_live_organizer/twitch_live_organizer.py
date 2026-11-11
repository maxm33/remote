import argparse
import errno
import json
import logging
import os
import re
import shutil
import sys
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

POLL_SECONDS = 5
STABLE_SECONDS = 15

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
LOGGER = logging.getLogger("twitch_live_organizer")


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

    def category_from_title(self, title):
        if not isinstance(title, str):
            return None
        title_folded = title.casefold()
        matches = (
            category
            for category in CATEGORY_TO_DIRECTORY
            if category.casefold() in title_folded
        )
        return max(matches, key=len, default=None)

    def video_by_id(self, video_id):
        params = urllib.parse.urlencode({"id": video_id})
        videos = self._get_json("{}/videos?{}".format(API_BASE, params))["data"]
        return videos[0] if videos else None


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


def validate_configuration():
    if not WATCH_FOLDER.is_dir():
        raise RuntimeError("Watch folder does not exist: {}".format(WATCH_FOLDER))
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "Set 'twitch_client_id' and 'twitch_client_secret' in {}.".format(
                CONFIG_PATH
            )
        )
    if not CATEGORY_TO_DIRECTORY:
        raise RuntimeError(
            "Add at least one category mapping to 'category_to_directory' in {}."
            .format(CONFIG_PATH)
        )
    for directory_name in CATEGORY_TO_DIRECTORY.values():
        if (
            not directory_name
            or Path(directory_name).name != directory_name
            or directory_name in (".", "..")
        ):
            raise RuntimeError(
                "Category directory names must be single folder names: {!r}".format(
                    directory_name
                )
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


def process_file(path, twitch):
    video_id = path.stem.split("-", 1)[0]
    video = twitch.video_by_id(video_id)
    if video is None:
        LOGGER.warning(
            "Twitch returned no VOD for video ID %s; leaving %s in place.",
            video_id,
            path,
        )
        return False
    title = video.get("title")
    if not isinstance(title, str) or not title.strip():
        LOGGER.warning(
            "Twitch returned no usable title for VOD %s; leaving %s in place.",
            video_id,
            path,
        )
        return False

    category = twitch.category_from_title(title)
    if category is None:
        destination_directory = OUTPUT_ROOT
        destination_name = title
        LOGGER.info(
            "No configured category matched VOD %s; using its title as the "
            "filename in %s.",
            video_id,
            OUTPUT_ROOT,
        )
    else:
        destination_name = CATEGORY_TO_DIRECTORY[category]
        destination_directory = OUTPUT_ROOT / destination_name

    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = next_destination(destination_directory, destination_name)
    move_without_overwriting(path, destination)
    if category is None:
        LOGGER.info("Moved %s to %s (no category matched).", path, destination)
    else:
        LOGGER.info("Moved %s to %s (category: %s)", path, destination, category)
    return True


def watch():
    validate_configuration()
    twitch = TwitchClient(CLIENT_ID, CLIENT_SECRET)
    observed = {}
    LOGGER.info("Watching %s for MP4 files.", WATCH_FOLDER)

    while True:
        now = time.monotonic()
        present = set()
        for path in WATCH_FOLDER.iterdir():
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
                if process_file(path, twitch):
                    observed.pop(path, None)
                else:
                    observed[path] = (size, now)
            except (OSError, RuntimeError, urllib.error.URLError, ValueError, KeyError):
                LOGGER.exception("Could not process %s; will retry.", path)
                observed[path] = (size, now)
        for path in set(observed) - present:
            observed.pop(path, None)
        time.sleep(POLL_SECONDS)


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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.install_startup:
        install_startup()
    elif args.uninstall_startup:
        uninstall_startup()
    else:
        watch()


if __name__ == "__main__":
    main()
