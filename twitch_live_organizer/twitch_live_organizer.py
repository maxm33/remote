import argparse
import getpass
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

if __package__:
    from .organizer_core.download_watcher import (
        DOWNLOAD_STAGING_DIRECTORY_NAME,
        watch,
    )
    from .organizer_core.instance_lock import (
        InstanceAlreadyRunningError,
        SingleInstanceLock,
    )
    from .organizer_core.logging_utils import ProgressFileHandler
else:
    from organizer_core.download_watcher import (
        DOWNLOAD_STAGING_DIRECTORY_NAME,
        watch,
    )
    from organizer_core.instance_lock import (
        InstanceAlreadyRunningError,
        SingleInstanceLock,
    )
    from organizer_core.logging_utils import ProgressFileHandler

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


OUTPUT_ROOT_VALUE = config_string("output_root")
if not OUTPUT_ROOT_VALUE.strip():
    raise RuntimeError("'output_root' in {} cannot be empty.".format(CONFIG_PATH))

OUTPUT_ROOT = Path(OUTPUT_ROOT_VALUE)
CLIENT_ID = config_string("twitch_client_id")
CLIENT_SECRET = config_string("twitch_client_secret")
CHANNEL_LOGIN = config_string("twitch_channel_login")

UPDATE_CHECK_SECONDS = 60 * 60

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

STARTUP_FILE_NAME = "TwitchLiveOrganizer.cmd"
LOGGER = logging.getLogger("twitch_live_organizer")
LOG_FILE_NAME = "organizer.log"


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


def configure_logging():
    log_file = app_data_directory() / LOG_FILE_NAME
    handler = ProgressFileHandler(log_file, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    return log_file


def clear_log_file(log_file):
    log_path = Path(log_file).resolve()
    handler = next(
        (
            handler
            for handler in LOGGER.handlers
            if isinstance(handler, ProgressFileHandler)
            and Path(handler.baseFilename).resolve() == log_path
        ),
        None,
    )
    if handler is None:
        raise RuntimeError("No active log handler found for {}.".format(log_path))
    handler.clear_log()


def open_log(log_file):
    escaped_path = str(log_file).replace("'", "''")
    organizer_pid = os.getpid()
    command = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$logPath = '{0}'; "
        "$organizerProcessId = {2}; "
        "$lastWrite = [datetime]::MinValue; $lastLength = -1L; "
        "$lastWidth = -1; $lastHeight = -1; "
        "$progressPattern = ' INFO VOD .* download: \\['; "
        "while (Get-Process -Id $organizerProcessId "
        "-ErrorAction SilentlyContinue) {{ "
        "$writeTime = [System.IO.File]::GetLastWriteTimeUtc($logPath); "
        "$length = (Get-Item -LiteralPath $logPath).Length; "
        "if ($writeTime -ne $lastWrite) {{ "
        "$allLines = @(Get-Content -LiteralPath $logPath -Tail 100 -Encoding UTF8); "
        "$progressLine = ''; "
        "$progressIndex = -1; $progressVideoId = ''; "
        "for ($index = $allLines.Count - 1; $index -ge 0; $index--) {{ "
        "if ($allLines[$index] -match $progressPattern) {{ "
        "$progressLine = $allLines[$index].TrimEnd(); "
        "$progressIndex = $index; "
        "if ($progressLine -match ' INFO VOD (\\S+) download: \\[') {{ "
        "$progressVideoId = $Matches[1] "
        "}}; "
        "break "
        "}} "
        "}}; "
        "if ($progressIndex -ge 0 -and $progressVideoId) {{ "
        "$finishedPattern = ' INFO Finished downloading Twitch VOD ' + "
        "[regex]::Escape($progressVideoId) + '\\.'; "
        "for ($index = $progressIndex + 1; "
        "$index -lt $allLines.Count; $index++) {{ "
        "if ($allLines[$index] -match $finishedPattern) {{ "
        "$progressLine = ''; break "
        "}} "
        "}} "
        "}}; "
        "$windowWidth = [Console]::WindowWidth; "
        "$windowHeight = [Console]::WindowHeight;\n"
        "$lineWidth = [Math]::Max(1, $windowWidth - 1);\n"
        "$progressRows = [int][Math]::Ceiling({1}.0 / $lineWidth);\n"
        "$progressRows = [Math]::Min([Math]::Max(1, $progressRows), $windowHeight);\n"
        "$progressChunks = @();\n"
        "for ($offset = 0; $offset -lt $progressLine.Length; $offset += $lineWidth) {{\n"
        "$chunkLength = [Math]::Min($lineWidth, $progressLine.Length - $offset);\n"
        "$progressChunks += $progressLine.Substring($offset, $chunkLength)\n"
        "}}; "
        "if ($progressChunks.Count -eq 0) {{ $progressChunks = @('') }};\n"
        "if ($length -ne $lastLength -or "
        "$windowWidth -ne $lastWidth -or $windowHeight -ne $lastHeight) {{ "
        "$logLines = @($allLines | Where-Object {{ $_ -notmatch $progressPattern }});\n"
        "$logRows = New-Object 'System.Collections.Generic.List[string]';\n"
        "foreach ($line in $logLines) {{\n"
        "$line = $line.TrimEnd();\n"
        "if ($line.Length -eq 0) {{\n"
        "[void]$logRows.Add('')\n"
        "}} else {{\n"
        "for ($offset = 0; $offset -lt $line.Length; $offset += $lineWidth) {{\n"
        "$chunkLength = [Math]::Min($lineWidth, $line.Length - $offset);\n"
        "[void]$logRows.Add($line.Substring($offset, $chunkLength))\n"
        "}}\n"
        "}}\n"
        "}}; "
        "$maxLogRows = [Math]::Max(0, $windowHeight - $progressRows);\n"
        "$firstLogRow = [Math]::Max(0, $logRows.Count - $maxLogRows);\n"
        "Clear-Host;\n"
        "$row = 0;\n"
        "for ($index = $firstLogRow;\n"
        "$index -lt $logRows.Count -and $row -lt $maxLogRows; $index++) {{\n"
        "[Console]::SetCursorPosition(0, $row); "
        "[Console]::Write($logRows[$index].PadRight($lineWidth));\n"
        "$row++\n"
        "}};\n"
        "$progressRow = [Math]::Min($row, $windowHeight - 1);\n"
        "for ($index = 0; $index -lt $progressRows; $index++) {{\n"
        "$statusChunk = '';\n"
        "if ($index -lt $progressChunks.Count) {{\n"
        "$statusChunk = $progressChunks[$index]\n"
        "}};\n"
        "[Console]::SetCursorPosition(0, $progressRow + $index);\n"
        "[Console]::Write($statusChunk.PadRight($lineWidth))\n"
        "}};\n"
        "[Console]::SetCursorPosition(0,\n"
        "[Math]::Min($progressRow + $progressRows, $windowHeight - 1))\n"
        "}} else {{ "
        "$cursorLeft = [Console]::CursorLeft; "
        "$cursorTop = [Console]::CursorTop; "
        "for ($index = 0; $index -lt $progressRows; $index++) {{\n"
        "$statusChunk = '';\n"
        "if ($index -lt $progressChunks.Count) {{\n"
        "$statusChunk = $progressChunks[$index]\n"
        "}};\n"
        "[Console]::SetCursorPosition(0, $progressRow + $index);\n"
        "[Console]::Write($statusChunk.PadRight($lineWidth))\n"
        "}};\n"
        "[Console]::SetCursorPosition($cursorLeft, $cursorTop) "
        "}}; "
        "$lastWrite = $writeTime; $lastLength = $length; "
        "$lastWidth = $windowWidth; $lastHeight = $windowHeight "
        "}}; Start-Sleep -Milliseconds 250 }}"
    ).format(
        escaped_path, ProgressFileHandler.PROGRESS_LINE_WIDTH, organizer_pid
    )
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-Command", command],
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


def watch_for_updates(stop_event):
    script_directory = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(script_directory),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        repository_root = Path(result.stdout.strip())
    except (OSError, subprocess.SubprocessError) as error:
        LOGGER.warning(
            "Automatic updates unavailable: %s",
            error,
        )
        return

    LOGGER.info("Checking for updates...")

    def check_for_updates():
        try:
            previous_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repository_root),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip()
            pull_result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=str(repository_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if pull_result.returncode:
                details = pull_result.stderr.strip() or pull_result.stdout.strip()
                LOGGER.warning(
                    "Update pull failed; will retry at the next hourly check: %s", details
                )
                return

            current_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repository_root),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip()
            if current_commit != previous_commit:
                LOGGER.info(
                    "Update found and applied; restarting.",
                )
                os.execv(sys.executable, [sys.executable] + sys.argv)
            LOGGER.info("No updates available.")
        except (OSError, subprocess.SubprocessError) as error:
            LOGGER.error(
                "Update check failed; will retry at the next hourly check: %s", error
            )

    if not stop_event.is_set():
        check_for_updates()
    while not stop_event.wait(UPDATE_CHECK_SECONDS):
        check_for_updates()


def start_update_watcher(stop_event):
    updater = threading.Thread(
        target=watch_for_updates,
        args=(stop_event,),
        name="twitch-live-organizer-updater",
        daemon=True,
    )
    updater.start()


def archive_state_path():
    return app_data_directory() / "archive-state.json"


def watch_organizer(stop_event, resume_event):
    watch(
        stop_event,
        resume_event,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        channel_login=CHANNEL_LOGIN,
        download_directory=(
            Path(__file__).resolve().parent / DOWNLOAD_STAGING_DIRECTORY_NAME
        ),
        output_root=OUTPUT_ROOT,
        category_to_directory=CATEGORY_TO_DIRECTORY,
        archive_state_path=archive_state_path(),
        logger=LOGGER,
    )


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
    start_update_watcher(stop_event)

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

    def clear_log_action(_icon, _item):
        try:
            clear_log_file(log_file)
        except (OSError, RuntimeError) as error:
            LOGGER.error("Could not clear the log file: %s", error)
        else:
            LOGGER.info("Log cleared by user.")

    def kill_app(icon, _item):
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
            pystray.MenuItem("Clear log", clear_log_action),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(pause_text, toggle_pause),
            pystray.MenuItem("Kill", kill_app),
        ),
    )

    def run_watcher():
        try:
            watch_organizer(stop_event, resume_event)
        except Exception as error:
            LOGGER.error("Watcher stopped unexpectedly: %s", error)
            stop_event.set()
            icon.stop()

    watcher = threading.Thread(
        target=run_watcher, name="twitch-live-organizer", daemon=True
    )
    watcher.start()
    try:
        icon.run()
    finally:
        stop_event.set()
        resume_event.set()


def validate_configuration():
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

    category_names = {}
    for category in CATEGORY_TO_DIRECTORY:
        if not category.strip():
            raise RuntimeError(
                "Category names in 'category_to_directory' cannot be empty."
            )
        category_key = category.casefold()
        if category_key in category_names:
            raise RuntimeError(
                "Category names in 'category_to_directory' must be unique "
                "ignoring case: {!r} and {!r}.".format(
                    category_names[category_key], category
                )
            )
        category_names[category_key] = category

    directory_names = {}
    for category, directory_name in CATEGORY_TO_DIRECTORY.items():
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
        directory_key = relative_directory.name.casefold()
        if directory_key in directory_names:
            raise RuntimeError(
                "Category directories in 'category_to_directory' must have "
                "unique names ignoring case: {!r} and {!r} both use {!r}.".format(
                    directory_names[directory_key],
                    category,
                    relative_directory.name,
                )
            )
        directory_names[directory_key] = category


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
        try:
            with SingleInstanceLock(app_data_directory() / "organizer.lock"):
                configure_twitch_credentials()
                log_file = configure_logging()
                LOGGER.info("Starting Twitch Live Organizer.")
                try:
                    validate_configuration()
                except Exception as error:
                    LOGGER.error(
                        "Invalid configuration; watcher not started: %s", error
                    )
                    raise SystemExit(1)
                if os.name == "nt":
                    run_tray(log_file)
                else:
                    LOGGER.info(
                        "Tray controls are available on Windows; running in foreground."
                    )
                    stop_event = threading.Event()
                    resume_event = threading.Event()
                    resume_event.set()
                    start_update_watcher(stop_event)
                    try:
                        watch_organizer(stop_event, resume_event)
                    finally:
                        stop_event.set()
        except InstanceAlreadyRunningError as error:
            LOGGER.error("%s", error)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
