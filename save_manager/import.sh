#!/usr/bin/env bash

set -e

if [[ $# -lt 2 || -z "$1" || "$1" == */* ]]; then
    echo "Usage: $0 <game-directory> <file> [file ...]" >&2
    exit 1
fi

GAME="$1"
shift
FILES=("$@")

for FILE in "${FILES[@]}"; do
    if [[ -z "$FILE" || "$FILE" == */* ]]; then
        echo "File arguments must be file names, not paths." >&2
        exit 1
    fi
done

SOURCE="$USERPROFILE/Desktop/remote/save_manager/saves/$GAME/76561197960271872"
DEST="$USERPROFILE/AppData/Roaming/$GAME/76561197960271872"

echo
echo "Updating savefiles..."
echo

git pull

echo
echo "Checking savefile timestamps..."
echo

for FILE in "${FILES[@]}"; do
    file_src="$SOURCE/$FILE"
    file_dst="$DEST/$FILE"

    if [[ -f "$file_src" && -f "$file_dst" ]]; then
        if [[ "$file_dst" -nt "$file_src" ]]; then
            echo "Aborted: '$FILE' in local destination is newer than the remote source."
            exit 1
        fi
    fi
done

echo "Copying savefiles from remote to local..."
echo

for FILE in "${FILES[@]}"; do
    cp -f "$SOURCE/$FILE" "$DEST/"
done

echo
echo "Done."
