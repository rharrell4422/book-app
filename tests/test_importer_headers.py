import unittest
from datetime import date

from importer.importer import import_row


class ImporterHeaderMappingTest(unittest.TestCase):
    def test_spreadsheet_headers_map_to_expected_fields(self):
        raw_headers = [
            "Titles",
            "Authors",
            "Read Status",
            "Date Read",
            "Next Release Date",
            "Series Names",
            "Book #",
            "Series Finished",
        ]
        row_values = [
            "Example Book",
            "Jane Doe",
            "Upcoming",
            "2026-06-28",
            "2026-07-14",
            "Example Series",
            1.0,
            "No",
        ]

        book_data, unknown_data = import_row(raw_headers, row_values)

        self.assertEqual(book_data["title"], "Example Book")
        self.assertEqual(book_data["author"], "Jane Doe")
        self.assertEqual(book_data["read_status"], "upcoming")
        self.assertEqual(book_data["date_finished"], date(2026, 6, 28))
        self.assertEqual(book_data["release_date"], date(2026, 7, 14))
        self.assertEqual(book_data["series_name"], "Example Series")
        self.assertEqual(book_data["book_number"], 1.0)
        self.assertEqual(book_data["series_finished"], "No")
        self.assertEqual(unknown_data, {})


if __name__ == "__main__":
    unittest.main()
