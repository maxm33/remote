#!/usr/bin/env bash

set -e

USER_HOME="$USERPROFILE"

SOURCE="$USER_HOME/AppData/Roaming/EldenRing/76561197960271872"
DEST="$USER_HOME/Desktop/remote/saves/76561197960271872"
REPO="$USER_HOME/Desktop/remote"

echo "Copying savefiles..."

cp -rf "$SOURCE/"* "$DEST/"

cd "$REPO"

git add --all

COMMIT_MSG=$(printf "%06d" $(( RANDOM % 1000000 )))

git commit -m "$COMMIT_MSG"

git push

echo "Done."