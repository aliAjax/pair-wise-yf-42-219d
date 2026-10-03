import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(repo, RuleEngine())
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), "")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None, user="admin", role="admin"):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=data,
            method=method,
        )
        request.add_header("X-User-Id", user)
        request.add_header("X-Role", role)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_pedigree_revisions_reviews_and_retry_endpoints(self):
        _, gs = self._request("POST", "/api/animals",
                              {"id": "gs", "name": "GS", "sex": "male",
                               "effective_date": "2025-01-01"})
        _, sire = self._request("POST", "/api/animals",
                                {"id": "sire", "name": "S", "sex": "male",
                                 "effective_date": "2025-01-01"})
        _, dam = self._request("POST", "/api/animals",
                               {"id": "dam", "name": "D", "sex": "female",
                                "effective_date": "2025-01-01"})
        _, pairing = self._request("POST", "/api/pairings",
                                   {"proposed_by": "coord"},
                                   user="coord", role="coordinator")
        status, approved = self._request(
            "POST", "/api/entities/%s/actions" % pairing["id"],
            {"action": "approve",
             "data": {"sire_id": sire["id"], "dam_id": dam["id"],
                      "approvals": ["vet"]}},
            user="coord", role="coordinator",
        )
        self.assertEqual(status, 200)
        self.assertEqual(approved["status"], "approved")

        # approval history carries the snapshot
        status, approvals = self._request(
            "GET", "/api/pairings/%s/approvals" % pairing["id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(approvals["items"]), 1)

        # pedigree as of a date
        status, tree = self._request(
            "GET", "/api/animals/sire/pedigree?date=2026-06-01"
        )
        self.assertEqual(status, 200)
        self.assertIsNone(tree["root"]["sire"])
        status, _ = self._request("GET", "/api/animals/sire/pedigree")
        self.assertEqual(status, 400)

        # registrar back-dates the father: approved pairing moves to review
        status, _ = self._request(
            "POST", "/api/entities/sire/actions",
            {"action": "revise",
             "data": {"sire_id": "gs", "reason": "paperwork",
                      "effective_date": "2025-01-01"},
             "expected_version": 1},
            user="reg", role="registrar",
        )
        self.assertEqual(status, 200)
        status, reviews = self._request("GET", "/api/pending_reviews")
        self.assertEqual(status, 200)
        self.assertEqual(len(reviews["items"]), 1)
        self.assertEqual(reviews["items"][0]["pairing_id"], pairing["id"])
        self.assertEqual(reviews["items"][0]["pairing"]["status"], "needs_review")
        self.assertIn("changed after approval", reviews["items"][0]["reason"])

        # version conflict: stale client is rejected with 409
        status, conflict = self._request(
            "POST", "/api/entities/sire/actions",
            {"action": "revise",
             "data": {"name": "S-2", "reason": "late"},
             "expected_version": 1},
            user="reg", role="registrar",
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["type"], "ConflictError")

        # recalculated pedigree now contains the new grandfather
        status, tree2 = self._request(
            "GET", "/api/animals/sire/pedigree?date=2026-06-01"
        )
        self.assertEqual(tree2["root"]["sire"]["id"], "gs")

        # coordinator retries the review through the API
        status, retried = self._request(
            "POST", "/api/pairings/%s/retry" % pairing["id"], {},
            user="coord", role="coordinator",
        )
        self.assertEqual(status, 200)
        self.assertEqual(retried["status"], "approved")

        # revision history of the sire
        status, revisions = self._request(
            "GET", "/api/animals/sire/revisions"
        )
        self.assertEqual(status, 200)
        self.assertEqual([r["version"] for r in revisions["items"]], [1, 2])

    def test_viewer_cannot_revise(self):
        self._request("POST", "/api/animals",
                      {"id": "a1", "name": "A", "sex": "male"})
        status, error = self._request(
            "POST", "/api/entities/a1/actions",
            {"action": "revise", "data": {"name": "A2", "reason": "x"}},
            user="v", role="viewer",
        )
        self.assertEqual(status, 403)
        self.assertEqual(error["type"], "PermissionDenied")


if __name__ == "__main__":
    unittest.main()
