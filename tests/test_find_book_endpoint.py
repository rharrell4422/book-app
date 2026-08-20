import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from routers.deps import create_owner_token


class FindBookEndpointTest(unittest.TestCase):
    """GET /books/find -- the FIND rebuild's public endpoint (see
    services/find_engine.py), and GET /books/lookup's book_number/
    series_name forwarding fix (previously silently dropped -- see the
    project design chat's consolidated Add Book specification).
    """

    def setUp(self):
        self.client = TestClient(main.app)
        self.token = create_owner_token()

    def test_requires_owner_auth(self):
        response = self.client.get("/books/find", params={"title": "Fourth Wing"})
        self.assertEqual(response.status_code, 401)

    def test_forwards_all_four_query_params_to_find_book_candidates(self):
        with patch("routers.books.find_book_candidates", return_value={"query": {}, "candidates": [], "provider_failures": []}) as mock_find:
            response = self.client.get(
                "/books/find",
                headers={"Authorization": f"Bearer {self.token}"},
                params={"title": "Fourth Wing", "author": "Rebecca Yarros", "book_number": 1, "series_name": "The Empyrean"},
            )

        self.assertEqual(response.status_code, 200)
        mock_find.assert_called_once_with("Fourth Wing", "Rebecca Yarros", 1.0, "The Empyrean")

    def test_lookup_route_forwards_book_number_and_series_name(self):
        # Regression: this route used to only forward title/author to
        # lookup_book_summary, silently dropping book_number/series_name
        # even though that function accepts and uses them to disambiguate
        # same-titled results from different volumes.
        with patch("routers.books.lookup_book_summary", return_value={"found": False}) as mock_lookup:
            response = self.client.get(
                "/books/lookup",
                headers={"Authorization": f"Bearer {self.token}"},
                params={"title": "Some Title", "author": "Some Author", "book_number": 4, "series_name": "Some Series"},
            )

        self.assertEqual(response.status_code, 200)
        mock_lookup.assert_called_once_with("Some Title", "Some Author", 4.0, "Some Series")


if __name__ == "__main__":
    unittest.main()
