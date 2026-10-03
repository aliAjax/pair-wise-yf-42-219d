import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository, utcnow
from src.rules import RuleEngine
from src.service import DomainService

ADMIN = Actor("admin", "admin")
COORD = Actor("coord", "coordinator")
REGISTRAR = Actor("reg", "registrar")


def make_service(db_path):
    return DomainService(SQLiteRepository(db_path), RuleEngine())


def create_animal(service, name, sex, animal_id, **extra):
    data = {"id": animal_id, "name": name, "sex": sex}
    data.update(extra)
    return service.create(ADMIN, "animal", data)


def approve_pairing(service, sire_id, dam_id):
    pairing = service.create(COORD, "pairing", {"proposed_by": "coord"})
    return service.transition(
        COORD,
        pairing["id"],
        "approve",
        {"sire_id": sire_id, "dam_id": dam_id, "approvals": ["vet-1"]},
    )


def invalidate_via_sire_revision(service, new_sire="gs"):
    create_animal(service, "GS", "male", new_sire)
    return service.transition(
        REGISTRAR,
        "sire",
        "revise",
        {"sire_id": new_sire, "reason": "late father record"},
        1,
    )


class ReviewQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.service = make_service(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _approved(self):
        create_animal(self.service, "S", "male", "sire")
        create_animal(self.service, "D", "female", "dam")
        return approve_pairing(self.service, "sire", "dam")

    def test_sweep_reapproves_with_fresh_versions_and_new_snapshot(self):
        pairing = self._approved()
        first = self.service.approvals(pairing["id"])[0]
        self.assertEqual(first["sire_version"], 1)

        invalidate_via_sire_revision(self.service)
        self.assertEqual(
            self.service.get(pairing["id"])["status"], "needs_review"
        )

        processed = self.service.process_due_reviews()
        self.assertEqual(processed, [pairing["id"]])
        updated = self.service.get(pairing["id"])
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(updated["data"]["sire_id"], "sire")

        approvals = self.service.approvals(pairing["id"])
        self.assertEqual([a["decision"] for a in approvals], ["approved", "approved"])
        self.assertFalse(approvals[0]["active"])
        self.assertTrue(approvals[-1]["active"])
        self.assertEqual(approvals[-1]["sire_version"], 2)
        self.assertEqual(approvals[-1]["dam_version"], 1)
        self.assertEqual(approvals[-1]["pedigree"]["sire"]["sire"]["id"], "gs")

        self.assertEqual(self.service.pending_reviews(), [])

    def test_rule_failure_keeps_item_blocked_with_reason_until_manual_retry(self):
        pairing = self._approved()
        invalidate_via_sire_revision(self.service)
        # make the decision fail the active-animal rule
        self.service.transition(
            ADMIN, "dam", "quarantine_animal", {"reason": "disease watch"}
        )

        processed = self.service.process_due_reviews()
        self.assertEqual(processed, [])

        items = self.service.pending_reviews()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "blocked")
        self.assertIn("active", items[0]["last_error"])

        # fixing the data lets the manual retry succeed
        self.service.transition(
            ADMIN, "dam", "release_quarantine", {}
        )
        result = self.service.retry_review(COORD, pairing["id"])
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self.service.pending_reviews(), [])

    def test_transient_failure_is_retried_on_next_sweep(self):
        self._approved()
        invalidate_via_sire_revision(self.service)

        calls = {"n": 0}
        original = self.service.rules.evaluate_pairing

        def flaky(actor, sire, dam):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database locked")
            return original(actor, sire, dam)

        self.service.rules.evaluate_pairing = flaky
        first_sweep = self.service.process_due_reviews()
        self.assertEqual(first_sweep, [])
        items = self.service.pending_reviews()
        self.assertEqual(items[0]["status"], "pending")
        self.assertIn("database locked", items[0]["last_error"])
        self.assertGreaterEqual(items[0]["attempts"], 1)

        # retry succeeds once the transient problem is gone
        self.service.rules.evaluate_pairing = original
        second_sweep = self.service.process_due_reviews()
        self.assertEqual(len(second_sweep), 1)

    def test_restart_resumes_stuck_processing_item(self):
        pairing = self._approved()
        invalidate_via_sire_revision(self.service)
        # simulate a crash mid-calculation
        self.service.repository.claim_due_reviews(limit=1)
        with self.service.repository._connect() as connection:
            connection.execute(
                "UPDATE review_queue SET status = 'processing' WHERE pairing_id = ?",
                (pairing["id"],),
            )
        self.assertEqual(
            self.service.pending_reviews()[0]["status"], "processing"
        )

        # new process on the same database: recovers and finishes the item
        restarted = make_service(self.db_path)
        restarted.repository.recover_stale_reviews()
        processed = restarted.process_due_reviews()
        self.assertEqual(processed, [pairing["id"]])
        self.assertEqual(restarted.get(pairing["id"])["status"], "approved")

    def test_retry_conflicts_for_items_not_in_review(self):
        pairing = self._approved()
        # still approved: nothing to retry
        with self.assertRaises(ConflictError):
            self.service.retry_review(COORD, pairing["id"])

    def test_manual_recheck_transition_records_new_decision(self):
        pairing = self._approved()
        invalidate_via_sire_revision(self.service)
        rechecked = self.service.transition(COORD, pairing["id"], "recheck", {})
        self.assertEqual(rechecked["status"], "approved")
        approvals = self.service.approvals(pairing["id"])
        self.assertEqual(len(approvals), 2)
        self.assertTrue(approvals[-1]["active"])
        self.assertEqual(approvals[-1]["note"], "manual recheck")

    def test_viewer_cannot_trigger_retry(self):
        pairing = self._approved()
        invalidate_via_sire_revision(self.service)
        from src.domain import PermissionDenied

        with self.assertRaises(PermissionDenied):
            self.service.retry_review(Actor("x", "viewer"), pairing["id"])


class BaselineMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.service = make_service(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pre_revision_animals_get_starting_revision(self):
        now = utcnow()
        # insert legacy rows as an older version of the app would
        with self.service.repository._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, "
                "created_at, updated_at) VALUES "
                "('old-sire', 'animal', 'active', 3, ?, 'legacy', ?, ?), "
                "('old-dam', 'animal', 'deceased', 1, ?, 'legacy', ?, ?)",
                (
                    '{"name": "Old S", "sex": "male"}', now, now,
                    '{"name": "Old D", "sex": "female"}', now, now,
                ),
            )
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, "
                "created_at, updated_at) VALUES "
                "('old-pair', 'pairing', 'approved', 1, ?, 'legacy', ?, ?)",
                (
                    '{"proposed_by": "c", "sire_id": "old-sire", '
                    '"dam_id": "old-dam", "approvals": ["vet"]}', now, now,
                ),
            )

        # restart: schema/data upgrade runs automatically
        restarted = make_service(self.db_path)

        sire_revisions = restarted.revisions("old-sire")
        self.assertEqual(len(sire_revisions), 1)
        self.assertEqual(sire_revisions[0]["version"], 3)
        self.assertTrue(sire_revisions[0]["reason"].startswith("baseline"))
        dam_revisions = restarted.revisions("old-dam")
        self.assertEqual(dam_revisions[0]["version"], 1)
        self.assertEqual(dam_revisions[0]["status"], "deceased")

        # baseline supports date-based pedigree rebuild
        tree = restarted.pedigree_as_of("old-sire", now[:10])
        self.assertEqual(tree["root"]["id"], "old-sire")
        self.assertEqual(tree["root"]["version"], 3)

        # pre-existing approved pairing gets a reconstructed approval record,
        # and a later parentage change still invalidates it
        approvals = restarted.approvals("old-pair")
        self.assertEqual(len(approvals), 1)
        self.assertTrue(approvals[0]["active"])
        self.assertEqual(approvals[0]["sire_version"], 3)

        create_animal(restarted, "GS", "male", "gs")
        restarted.transition(
            REGISTRAR,
            "old-sire",
            "revise",
            {"sire_id": "gs", "reason": "late paperwork"},
            3,
        )
        self.assertEqual(restarted.get("old-pair")["status"], "needs_review")
        items = restarted.pending_reviews()
        self.assertEqual(len(items), 1)
        self.assertIn("version 3", items[0]["reason"])

    def test_migration_is_idempotent(self):
        create_animal(self.service, "S", "male", "sire")
        make_service(self.db_path)  # restart once
        make_service(self.db_path)  # restart twice
        self.assertEqual(
            len(DomainService(SQLiteRepository(self.db_path), RuleEngine())
                .revisions("sire")),
            1,
        )


if __name__ == "__main__":
    unittest.main()
