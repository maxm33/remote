import unittest
from unittest import mock

from twitch_live_organizer import twitch_live_organizer


class ConfigurationTests(unittest.TestCase):
    def validate_categories(self, category_to_directory):
        with mock.patch.multiple(
            twitch_live_organizer,
            CLIENT_ID="client-id",
            CLIENT_SECRET="client-secret",
            CHANNEL_LOGIN="channel",
            CATEGORY_TO_DIRECTORY=category_to_directory,
        ):
            twitch_live_organizer.validate_configuration()

    def test_rejects_empty_or_whitespace_category_names(self):
        for category in ("", "  "):
            with self.subTest(category=category):
                with self.assertRaisesRegex(RuntimeError, "cannot be empty"):
                    self.validate_categories({category: "Games"})

    def test_rejects_case_insensitive_duplicate_category_names(self):
        with self.assertRaisesRegex(RuntimeError, "unique ignoring case"):
            self.validate_categories({"Game": "Games", "game": "Other"})

    def test_rejects_case_insensitive_duplicate_directory_names(self):
        with self.assertRaisesRegex(RuntimeError, "unique names ignoring case"):
            self.validate_categories(
                {
                    "Arcade": "Games/Arcade",
                    "Platformer": "Other/arcade",
                }
            )

    def test_accepts_categories_with_unique_names(self):
        self.validate_categories({"Game": "Games", "Film": "Films"})


if __name__ == "__main__":
    unittest.main()
