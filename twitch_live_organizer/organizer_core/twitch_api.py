import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_BASE = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


@dataclass(frozen=True)
class PendingDownload:
    video_id: str
    path: Path


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


def load_archive_state(state_path):
    if not state_path.exists():
        return False, None, []
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

    pending_downloads = state.get("pending_downloads", [])
    if not isinstance(pending_downloads, list):
        raise RuntimeError("Invalid pending downloads in {}.".format(state_path))
    parsed_downloads = []
    for item in pending_downloads:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("video_id"), str)
            or not isinstance(item.get("path"), str)
        ):
            raise RuntimeError("Invalid pending downloads in {}.".format(state_path))
        parsed_downloads.append(
            PendingDownload(item["video_id"], Path(item["path"]))
        )
    return True, state["last_video_id"], parsed_downloads


def save_archive_state(state_path, video_id, pending_downloads):
    temporary_path = state_path.with_suffix(".tmp")
    state = {
        "initialized": True,
        "last_video_id": video_id,
        "pending_downloads": [
            {"video_id": item.video_id, "path": str(item.path)}
            for item in pending_downloads
        ],
    }
    with temporary_path.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file)
    temporary_path.replace(state_path)
