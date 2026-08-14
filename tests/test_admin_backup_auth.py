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
