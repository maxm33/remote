#!/usr/bin/env bash

set -e

SOURCE="$USERPROFILE/AppData/Roaming/EldenRing/76561197960271872"
DEST="$USERPROFILE/Desktop/remote/saves/EldenRing/76561197960271872"
REPO="$USERPROFILE/Desktop/remote"

echo
echo "Copying savefiles from local to remote..."
echo
echo

cp -rf "$SOURCE/"* "$DEST/"

cd "$REPO"

git add --all

message=$(printf "%06d" $(( RANDOM % 1000000 )))

git commit --quiet -m "$message"

git push

echo
echo
echo "Done."
