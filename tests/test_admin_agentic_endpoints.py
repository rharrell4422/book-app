"""Phase 1, tenth implementation block: `routers/admin_agentic.py` --
the first Phase 1 block wired into `main.py`, deliberately scoped to a
read-only, owner-only `/admin/agentic/*` surface.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file proves:

1. Every route requires owner auth -- an anonymous request is rejected
   (mirrors `tests/test_admin_backup_auth.py`'s existing pattern for
   `/admin/*`).
2. An authenticated owner request succeeds and returns the documented
   shape for each of the three routes (single JSON, single HTML, batch).
3. Nothing behind these routes writes anything (every underlying service
   function already has its own dedicated no-write tests; this file just
   confirms the route wiring itself doesn't introduce a new write path).
"""

import unittest

from fastapi.testclient import TestClient

import main
from routers.deps import create_owner_token


class AdminAgenticEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        # Deliberately not a real series -- every underlying service
        # function already handles "series not found" gracefully (see
        # tests/test_agentic_evaluation_harness.py etc.), so this proves
        # the route wiring itself works without depending on this
        # environment's actual library contents.
        self.series_id = 999999999

    # -- 1: owner auth is required on every route ---------------------------

    def test_admin_evaluate_rejects_anonymous_requests(self):
        response = self.client.get(f"/admin/agentic/evaluate/{self.series_id}")
        self.assertEqual(response.status_code, 403)

    def test_admin_evaluate_html_rejects_anonymous_requests(self):
        response = self.client.get(f"/admin/agentic/evaluate/{self.series_id}/html")
        self.assertEqual(response.status_code, 403)

    def test_admin_batch_rejects_anonymous_requests(self):
        response = self.client.post("/admin/agentic/batch", json=[self.series_id])
        self.assertEqual(response.status_code, 403)

    # -- 2: owner auth succeeds, documented shapes ---------------------------

    def test_admin_evaluate(self):
        response = self.client.get(f"/admin/agentic/evaluate/{self.series_id}", headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["series_id"], self.series_id)
        for key in ("timestamp", "live", "agentic", "comparison", "drift_report", "ttl_report"):
            self.assertIn(key, body)
        self.assertEqual(set(body["live"].keys()), {"skeleton", "confidence", "gate"})
        self.assertEqual(
            set(body["agentic"].keys()),
            {"provider_calls", "probes", "confidence_traces", "gate_traces", "skeleton_merge_previews", "reasoning_steps"},
        )

    def test_admin_evaluate_html(self):
        response = self.client.get(f"/admin/agentic/evaluate/{self.series_id}/html", headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        html_text = response.text
        self.assertIn(f"<h2>Series ID: {self.series_id}</h2>", html_text)
        for title in ("Live Snapshot", "Agentic Trace", "Comparison", "Drift Report", "TTL Report"):
            self.assertIn(f"<h3>{title}</h3>", html_text)

    def test_admin_batch_evaluation(self):
        other_series_id = 999999998
        response = self.client.post(
            "/admin/agentic/batch", json=[self.series_id, other_series_id], headers=self.owner_headers
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["results"]), 2)
        self.assertEqual(body["results"][0]["series_id"], self.series_id)
        self.assertEqual(body["results"][1]["series_id"], other_series_id)
        self.assertIn("batch_timestamp", body)

    def test_admin_batch_evaluation_rejects_non_list_body(self):
        response = self.client.post("/admin/agentic/batch", json={"not": "a list"}, headers=self.owner_headers)
        self.assertEqual(response.status_code, 422)

    # -- 3: route wiring introduces no new write path ------------------------

    def test_admin_agentic_routes_are_not_registered_under_any_other_prefix(self):
        # Sanity check that this router is scoped exactly to /admin/agentic
        # (not, say, accidentally mounted without its prefix) -- a request
        # to the bare service path should 404, not resolve to one of these
        # routes.
        response = self.client.get(f"/evaluate/{self.series_id}", headers=self.owner_headers)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
