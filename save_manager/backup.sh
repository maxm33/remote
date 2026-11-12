#!/usr/bin/env bash

set -e

if [[ $# -ne 1 || -z "$1" || "$1" == */* ]]; then
    echo "Usage: $0 <game-directory>" >&2
    exit 1
fi

GAME="$1"
SOURCE="$USERPROFILE/AppData/Roaming/$GAME/76561197960271872"
DEST="$USERPROFILE/Desktop/remote/save_manager/saves/$GAME/76561197960271872"
REPO="$USERPROFILE/Desktop/remote"

echo
echo "Copying savefiles from local to remote..."
echo

cp -rf "$SOURCE/"* "$DEST/"

cd "$REPO"

git add --all

message=$(printf "%06d" $((RANDOM % 1000000)))

git commit --quiet -m "$message"

git push

echo
echo
echo "Done."
