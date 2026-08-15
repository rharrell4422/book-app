import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from routers.deps import create_owner_token


class SeriesOverviewEndpointTest(unittest.TestCase):
    """POST /books/series_overview -- the on-demand "Series Overview" call
    from the "More by this author" dialog. Takes descriptions the frontend
    already has in memory (from the discover_by_author response) rather
    than re-fetching anything, so the only backend work is one LLM call.
    """

    def setUp(self):
        self.client = TestClient(main.app)
        self.token = create_owner_token()

    def test_requires_owner_auth(self):
        response = self.client.post(
            "/books/series_overview",
            json={"series_name": "Exile", "author": "Glynn Stewart", "books": []},
        )
        self.assertEqual(response.status_code, 403)

    def test_returns_generated_overview_from_provided_descriptions(self):
        with patch(
            "routers.books.generate_series_overview", return_value="A space opera trilogy."
        ) as mock_generate:
            response = self.client.post(
                "/books/series_overview",
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "series_name": "Exile",
                    "author": "Glynn Stewart",
                    "books": [{"title": "Exile", "description": "A shackled Earth..."}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"overview": "A space opera trilogy."})
        mock_generate.assert_called_once_with(
            "Exile", "Glynn Stewart", [{"title": "Exile", "description": "A shackled Earth..."}]
        )

    def test_returns_null_overview_when_generation_is_unavailable(self):
        # e.g. ANTHROPIC_API_KEY not configured, or none of the provided
        # books had a usable description -- not an error, just nothing to
        # show.
        with patch("routers.books.generate_series_overview", return_value=None):
            response = self.client.post(
                "/books/series_overview",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"series_name": "Exile", "author": "Glynn Stewart", "books": []},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"overview": None})


if __name__ == "__main__":
    unittest.main()
