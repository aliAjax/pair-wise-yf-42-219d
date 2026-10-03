import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

ADMIN = Actor("admin", "admin")
COORD = Actor("coord", "coordinator")
REGISTRAR = Actor("reg", "registrar")
VIEWER = Actor("viewer", "viewer")


def make_service():
    tmp = tempfile.TemporaryDirectory()
    repo = SQLiteRepository(Path(tmp.name) / "test.db")
    return DomainService(repo, RuleEngine()), tmp


def create_animal(service, name, sex, animal_id=None, **extra):
    data = {"name": name, "sex": sex}
    if animal_id:
        data["id"] = animal_id
    data.update(extra)
    return service.create(ADMIN, "animal", data)


def propose_and_approve(service, sire_id, dam_id, actor=COORD):
    pairing = service.create(actor, "pairing", {"proposed_by": actor.user_id})
    return service.transition(
        actor,
        pairing["id"],
        "approve",
        {"sire_id": sire_id, "dam_id": dam_id, "approvals": ["vet-1"]},
    )


class RevisionAndPedigreeTest(unittest.TestCase):
    def setUp(self):
        self.service, self.tmp = make_service()

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_writes_starting_revision(self):
        animal = create_animal(self.service, "M-1", "male", animal_id="m1")
        revisions = self.service.revisions("m1")
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["version"], 1)
        self.assertEqual(revisions[0]["reason"], "registered")
        self.assertEqual(animal["version"], 1)

    def test_revision_is_saved_with_effective_date_and_version_bumps(self):
        create_animal(self.service, "M-1", "male", animal_id="m1")
        updated = self.service.transition(
            REGISTRAR,
            "m1",
            "revise",
            {
                "sire_id": "gs1",
                "reason": "paper certificate arrived",
                "effective_date": "2026-05-01",
            },
            expected_version=1,
        )
        self.assertEqual(updated["version"], 2)
        revisions = self.service.revisions("m1")
        self.assertEqual([r["version"] for r in revisions], [1, 2])
        self.assertEqual(revisions[1]["effective_date"], "2026-05-01")
        self.assertEqual(revisions[1]["data"]["sire_id"], "gs1")
        # the older revision still carries the original parent data
        self.assertNotIn("sire_id", revisions[0]["data"])

    def test_revision_requires_reason_and_change(self):
        create_animal(self.service, "M-1", "male", animal_id="m1")
        with self.assertRaises(ValidationError):
            self.service.transition(
                REGISTRAR, "m1", "revise", {"reason": "nothing", "name": "M-1"}, 1
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                REGISTRAR, "m1", "revise", {"name": "M-2"}, 1
            )

    def test_pedigree_rebuilds_three_generations_with_the_days_version(self):
        create_animal(self.service, "GGS", "male", animal_id="ggs",
                      effective_date="2025-01-01")
        create_animal(self.service, "GS", "male", animal_id="gs",
                      effective_date="2025-01-01")
        create_animal(self.service, "GD", "female", animal_id="gd",
                      effective_date="2025-01-01")
        create_animal(self.service, "S1", "male", animal_id="s1",
                      effective_date="2025-01-01")
        create_animal(self.service, "D1", "female", animal_id="d1",
                      effective_date="2025-01-01")
        # old sire identity recorded for S1
        self.service.transition(
            REGISTRAR, "s1", "revise",
            {"sire_id": "gs", "reason": "birth record", "effective_date": "2025-06-01"}, 1
        )
        # parents for the grandparents are discovered much later and back-dated
        self.service.transition(
            REGISTRAR, "gs", "revise",
            {"sire_id": "ggs", "reason": "grandfather registered",
             "effective_date": "2025-01-01"}, 1
        )
        self.service.transition(
            REGISTRAR, "gd", "revise",
            {"sire_id": "ggs", "dam_id": "ggd",
             "reason": "grandparent certificates", "effective_date": "2025-01-01"}, 1
        )

        # Before the sire was recorded (2025-03-01): S1 has no sire
        early = self.service.pedigree_as_of("s1", "2025-03-01")["root"]
        self.assertEqual(early["version"], 1)
        self.assertIsNone(early["sire"])

        # After the corrections: three generations reconstruct with the day's versions
        tree = self.service.pedigree_as_of("s1", "2026-06-01")["root"]
        self.assertEqual(tree["version"], 2)
        self.assertEqual(tree["sire"]["id"], "gs")
        self.assertEqual(tree["sire"]["version"], 2)
        self.assertEqual(tree["sire"]["sire"]["id"], "ggs")
        self.assertEqual(tree["sire"]["sire"]["sire"], None)
        self.assertEqual(tree["dam"], None)

        gd_tree = self.service.pedigree_as_of("gd", "2026-06-01")["root"]
        self.assertEqual(gd_tree["version"], 2)
        self.assertEqual(gd_tree["sire"]["id"], "ggs")
        self.assertEqual(gd_tree["dam"]["id"], "ggd")
        self.assertEqual(gd_tree["dam"]["missing"], True)

        # Querying before the animal had any revision gives a null root
        none_root = self.service.pedigree_as_of("d1", "2000-01-01")
        self.assertIsNone(none_root["root"])


class PairingSnapshotInvalidationTest(unittest.TestCase):
    def setUp(self):
        self.service, self.tmp = make_service()

    def tearDown(self):
        self.tmp.cleanup()

    def _approved_pairing(self):
        create_animal(self.service, "S", "male", animal_id="sire")
        create_animal(self.service, "D", "female", animal_id="dam")
        pairing = propose_and_approve(self.service, "sire", "dam")
        return pairing

    def test_approval_records_versions_and_three_generation_snapshots(self):
        create_animal(self.service, "GS", "male", animal_id="gs",
                      effective_date="2025-01-01")
        create_animal(self.service, "S", "male", animal_id="sire",
                      effective_date="2025-01-01")
        create_animal(self.service, "D", "female", animal_id="dam",
                      effective_date="2025-01-01")
        self.service.transition(
            REGISTRAR, "sire", "revise",
            {"sire_id": "gs", "reason": "birth record",
             "effective_date": "2025-01-01"}, 1
        )
        pairing = propose_and_approve(self.service, "sire", "dam")
        approvals = self.service.approvals(pairing["id"])
        self.assertEqual(len(approvals), 1)
        record = approvals[0]
        self.assertTrue(record["active"])
        self.assertEqual(record["sire_id"], "sire")
        self.assertEqual(record["sire_version"], 2)
        self.assertEqual(record["dam_version"], 1)
        self.assertEqual(record["pedigree"]["sire"]["sire"]["id"], "gs")
        self.assertIn("inbreeding", record)

    def test_parentage_change_invalidates_approved_pairing_into_review(self):
        pairing = self._approved_pairing()
        self.assertEqual(pairing["status"], "approved")

        # unrelated edit (rename) must not invalidate
        renamed = self.service.transition(
            REGISTRAR, "sire", "revise",
            {"name": "S-II", "reason": "name correction",
             "effective_date": "2026-02-01"}, 1
        )
        pairing_after_rename = self.service.get(pairing["id"])
        self.assertEqual(pairing_after_rename["status"], "approved")

        # parentage change flips the approved pairing into needs_review
        create_animal(self.service, "GS", "male", animal_id="gs")
        self.service.transition(
            REGISTRAR, "sire", "revise",
            {"sire_id": "gs", "reason": "newly found father",
             "effective_date": "2026-03-01"}, renamed["version"]
        )
        pending = self.service.pending_reviews()
        self.assertEqual(len(pending), 1)
        item = pending[0]
        self.assertEqual(item["pairing_id"], pairing["id"])
        self.assertEqual(item["status"], "pending")
        self.assertIn("sire", item["reason"])
        self.assertIn("current version is 3", item["reason"])
        self.assertEqual(item["pairing"]["status"], "needs_review")

        # original conclusion and snapshot remain queryable, marked inactive
        approvals = self.service.approvals(pairing["id"])
        self.assertEqual(len(approvals), 1)
        self.assertFalse(approvals[0]["active"])
        self.assertIn("changed after approval", approvals[0]["superseded_reason"])

    def test_completed_pairing_is_touched_only_by_new_approvals(self):
        pairing = self._approved_pairing()
        self.service.transition(
            COORD, pairing["id"], "complete", {"offspring_ids": ["baby-1"]}
        )
        create_animal(self.service, "GS", "male", animal_id="gs")
        self.service.transition(
            REGISTRAR, "sire", "revise",
            {"sire_id": "gs", "reason": "late correction"}, 1
        )
        # completed pairings keep their historical conclusion, nothing queued
        self.assertEqual(self.service.get(pairing["id"])["status"], "completed")
        self.assertEqual(self.service.pending_reviews(), [])
        approvals = self.service.approvals(pairing["id"])
        self.assertTrue(approvals[0]["active"])

    def test_status_change_of_a_parent_sends_approved_pairing_to_review(self):
        pairing = self._approved_pairing()
        self.service.transition(
            ADMIN, "dam", "quarantine_animal", {"reason": "disease watch"}
        )
        pending = self.service.pending_reviews()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["pairing_id"], pairing["id"])
        self.assertIn("status changed to quarantined", pending[0]["reason"])

        # sweep cannot re-approve while quarantined: rule failure stays blocked
        self.assertEqual(self.service.process_due_reviews(), [])
        blocked = self.service.pending_reviews()[0]
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("active", blocked["last_error"])

        # releasing quarantine puts the pairing back into review, sweep reapproves
        self.service.transition(
            ADMIN, "dam", "release_quarantine", {}
        )
        processed = self.service.process_due_reviews()
        self.assertEqual(processed, [pairing["id"]])
        self.assertEqual(self.service.get(pairing["id"])["status"], "approved")

    def test_concurrent_revisions_leave_only_one_winner(self):
        create_animal(self.service, "S", "male", animal_id="sire")
        create_animal(self.service, "GS-A", "male", animal_id="gsa")
        create_animal(self.service, "GS-B", "male", animal_id="gsb")
        results = []
        errors = []

        def revise(sire_id):
            try:
                results.append(
                    self.service.transition(
                        REGISTRAR, "sire", "revise",
                        {"sire_id": sire_id, "reason": "concurrent edit",
                         "effective_date": "2026-04-01"}, 1
                    )
                )
            except ConflictError as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=revise, args=("gsa",)),
            threading.Thread(target=revise, args=("gsb",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        animal = self.service.get("sire")
        self.assertEqual(animal["version"], 2)
        self.assertIn(animal["data"]["sire_id"], ("gsa", "gsb"))
        revisions = self.service.revisions("sire")
        self.assertEqual(len(revisions), 2)

        # the loser retries against the fresh version and lands version 3
        winner_sire = animal["data"]["sire_id"]
        retry_sire = "gsb" if winner_sire == "gsa" else "gsa"
        again = self.service.transition(
            REGISTRAR, "sire", "revise",
            {"sire_id": retry_sire, "reason": "retry after conflict",
             "effective_date": "2026-04-02"}, 2
        )
        self.assertEqual(again["version"], 3)
        self.assertEqual(again["data"]["sire_id"], retry_sire)


if __name__ == "__main__":
    unittest.main()
