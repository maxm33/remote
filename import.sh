#!/usr/bin/env bash

set -e

SOURCE="$USERPROFILE/Desktop/remote/saves/EldenRing/76561197960271872"
DEST="$USERPROFILE/AppData/Roaming/EldenRing/76561197960271872"

FILES=("ER0000.sl2" "ER0000.sl2.bak")

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

cp -rf "$SOURCE/"* "$DEST/"

echo
echo "Done."
