import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from routers.deps import create_owner_token


class AdminBackupAuthTest(unittest.TestCase):
    """export_db should accept either normal owner auth or a valid
    X-Backup-Token header (for unattended scheduled backups), while every
    other /admin route should still require full owner auth only."""

    def setUp(self):
        self.client = TestClient(main.app)

    def test_export_db_rejects_anonymous_requests(self):
        response = self.client.get("/admin/export_db")
        self.assertEqual(response.status_code, 403)

    def test_export_db_accepts_owner_token(self):
        token = create_owner_token()
        response = self.client.get(
            "/admin/export_db", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 200)

    @patch.dict(os.environ, {"BACKUP_TOKEN": "test-backup-secret"})
    def test_export_db_accepts_matching_backup_token(self):
        import routers.deps as deps_module

        with patch.object(deps_module, "BACKUP_TOKEN", "test-backup-secret"):
            response = self.client.get(
                "/admin/export_db", headers={"X-Backup-Token": "test-backup-secret"}
            )
        self.assertEqual(response.status_code, 200)

    def test_export_db_rejects_wrong_backup_token(self):
        import routers.deps as deps_module

        with patch.object(deps_module, "BACKUP_TOKEN", "test-backup-secret"):
            response = self.client.get(
                "/admin/export_db", headers={"X-Backup-Token": "wrong-value"}
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Invalid backup token")

    def test_export_db_tolerates_trailing_whitespace_on_either_side(self):
        # A stray trailing newline from copy-pasting the token into an env
        # var UI (Railway, GitHub secrets, etc.) shouldn't break the match.
        import routers.deps as deps_module

        with patch.object(deps_module, "BACKUP_TOKEN", "test-backup-secret\n"):
            response = self.client.get(
                "/admin/export_db", headers={"X-Backup-Token": "test-backup-secret "}
            )
        self.assertEqual(response.status_code, 200)

    def test_export_db_reports_unconfigured_backup_token_distinctly(self):
        # If BACKUP_TOKEN isn't set on the server at all, sending the header
        # should still fail clearly rather than silently falling through to
        # the generic "log in as owner" message.
        import routers.deps as deps_module

        with patch.object(deps_module, "BACKUP_TOKEN", None):
            response = self.client.get(
                "/admin/export_db", headers={"X-Backup-Token": "anything"}
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Invalid backup token")

    def test_backup_token_does_not_grant_access_to_other_admin_routes(self):
        import routers.deps as deps_module

        with patch.object(deps_module, "BACKUP_TOKEN", "test-backup-secret"):
            response = self.client.post(
                "/admin/purge_orphaned_books",
                headers={"X-Backup-Token": "test-backup-secret"},
            )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
