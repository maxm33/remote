import errno
import os
import re
import shutil
from pathlib import Path


def move_without_overwriting(source, destination):
    try:
        os.rename(source, destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise

        created_destination = False
        try:
            with source.open("rb") as source_file:
                with destination.open("xb") as target_file:
                    created_destination = True
                    shutil.copyfileobj(source_file, target_file)
            shutil.copystat(source, destination)
            source.unlink()
        except OSError:
            if created_destination and destination.exists():
                destination.unlink()
            raise


class FileOrganizer:
    def __init__(self, output_root, category_to_directory, logger):
        self.output_root = Path(output_root)
        self.category_to_directory = category_to_directory
        self.logger = logger
        self._next_ids = {}

    def _scan_max_used_id(self, directory, safe_name):
        pattern = re.compile(
            r"^{} #(\d+)\.mp4$".format(re.escape(safe_name)),
            re.IGNORECASE,
        )
        used_ids = []
        for existing in directory.iterdir():
            match = pattern.match(existing.name)
            if match:
                used_ids.append(int(match.group(1)))
        return max(used_ids, default=0)

    def next_destination(self, directory, directory_name):
        safe_name = re.sub(r'[<>:"/\\|?*]', "_", directory_name).rstrip(" .")
        if not safe_name:
            raise RuntimeError("Directory name cannot produce a valid file name.")

        directory = Path(directory)
        directory_key = directory.resolve()
        cache_key = (directory_key, safe_name.casefold())
        directory_mtime = directory.stat().st_mtime_ns
        cached = self._next_ids.get(cache_key)
        if cached is None or cached[1] != directory_mtime:
            next_id = self._scan_max_used_id(directory, safe_name) + 1
        else:
            next_id = cached[0]

        while True:
            destination = directory / "{} #{}.mp4".format(safe_name, next_id)
            next_id += 1
            if not destination.exists():
                self._next_ids[cache_key] = (next_id, directory_mtime)
                return destination

    def _record_directory_change(self, directory):
        directory_key = directory.resolve()
        directory_mtime = directory.stat().st_mtime_ns
        for key, (next_id, _) in list(self._next_ids.items()):
            if key[0] == directory_key:
                self._next_ids[key] = (next_id, directory_mtime)

    def category_from_title(self, title):
        if not isinstance(title, str):
            return None
        title_folded = title.casefold()
        matches = (
            category
            for category in self.category_to_directory
            if category.casefold() in title_folded
        )
        return max(matches, key=len, default=None)

    def _category_from_directory_name(self, title):
        if not isinstance(title, str):
            return None
        title_folded = title.casefold()
        matches = (
            category
            for category, directory in self.category_to_directory.items()
            if Path(directory).name.casefold() == title_folded
        )
        return max(matches, key=len, default=None)

    def process_file(self, path):
        path = Path(path)
        filename_stem = path.stem
        _, separator, title = filename_stem.partition("-")
        if not separator:
            title = filename_stem
        if not title.strip():
            self.logger.warning(
                "Could not read a title from the downloaded filename; leaving %s in place.",
                path,
            )
            return False

        category = self.category_from_title(title)
        if category is None:
            category = self._category_from_directory_name(title)
        if category is None:
            destination_directory = self.output_root
            destination_name = title
            self.logger.info(
                "No configured category matched title %r; using it as the filename "
                "in %s.",
                title,
                self.output_root,
            )
        else:
            relative_directory = Path(self.category_to_directory[category])
            destination_name = relative_directory.name
            destination_directory = self.output_root / relative_directory

        destination_directory.mkdir(parents=True, exist_ok=True)
        while True:
            destination = self.next_destination(
                destination_directory, destination_name
            )
            try:
                move_without_overwriting(path, destination)
                self._record_directory_change(destination_directory)
                break
            except FileExistsError:
                self.logger.warning(
                    "Destination %s appeared during the move; choosing another name.",
                    destination,
                )

        if category is None:
            self.logger.info("Moved %s to %s (no category matched).", path, destination)
        else:
            self.logger.info(
                "Moved %s to %s (category: %s)", path, destination, category
            )
        return True
