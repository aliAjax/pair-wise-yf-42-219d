from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import RuleEngine, coefficient_from_trees, inbreeding_coefficient


SYSTEM_ACTOR = Actor("system", "admin")


class DomainService:
    REVIEW_THRESHOLD = 0.125
    MAX_REVIEW_ATTEMPTS = 5

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.repository.ensure_baseline_revisions()
        self.process_reviews()

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    @staticmethod
    def _today():
        return datetime.now(timezone.utc).date().isoformat()

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        effective_date = None
        if kind == "animal":
            effective_date = payload.pop("effective_date", None) or self._today()
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        if kind == "animal":
            self.repository.add_revision(
                entity_id, 1, effective_date, status, payload, actor.user_id
            )
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        kind = self.rules.normalize_kind(entity["kind"])

        if kind == "animal":
            effective_date = merged.pop("effective_date", None) or self._today()
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
            self.repository.add_revision(
                entity_id,
                updated["version"],
                effective_date,
                next_status,
                merged,
                actor.user_id,
            )
            self._maybe_invalidate_pairings(entity, merged, updated["version"])
        elif kind == "pairing" and action in ("approve", "reapprove"):
            snapshot = self._pedigree_snapshot(
                merged.get("sire_id"), merged.get("dam_id")
            )
            merged["pedigree_snapshot"] = snapshot
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        else:
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)

        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # --- revisions / pedigree ------------------------------------------

    def revisions(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self.repository.list_revisions(entity_id)

    def pedigree(self, animal_id, date=None, generations=3):
        date = date or self._today()
        try:
            generations = int(generations)
        except (TypeError, ValueError):
            generations = 3

        def build(current_id, gen):
            if current_id is None:
                return None
            revision = self.repository.get_revision_at(current_id, date)
            if not revision:
                return {
                    "animal": {"id": current_id, "known": False},
                    "sire": None,
                    "dam": None,
                }
            data = revision["data"]
            animal = {
                "id": current_id,
                "name": data.get("name"),
                "sex": data.get("sex"),
                "status": revision["status"],
                "revision": revision["revision"],
                "effective_date": revision["effective_date"],
                "known": True,
            }
            if gen <= 0:
                return {"animal": animal, "sire": None, "dam": None}
            return {
                "animal": animal,
                "sire": build(data.get("sire_id"), gen - 1),
                "dam": build(data.get("dam_id"), gen - 1),
            }

        return build(animal_id, generations)

    # --- pairing approval snapshots ------------------------------------

    def _pedigree_snapshot(self, sire_id, dam_id):
        sires = self._lookup("animal", "id", sire_id)
        dams = self._lookup("animal", "id", dam_id)
        sire = sires[0] if sires else None
        dam = dams[0] if dams else None
        if not sire or not dam:
            raise ValidationError("pairing requires two existing animals")
        coefficient = inbreeding_coefficient(sire["data"], dam["data"])
        return {
            "sire_id": sire_id,
            "sire_version": sire["version"],
            "dam_id": dam_id,
            "dam_version": dam["version"],
            "coefficient": coefficient,
            "approved_at": utcnow(),
        }

    def _maybe_invalidate_pairings(self, old_entity, new_data, new_version):
        if new_version <= 1:
            return
        old_sire = old_entity["data"].get("sire_id")
        old_dam = old_entity["data"].get("dam_id")
        new_sire = new_data.get("sire_id")
        new_dam = new_data.get("dam_id")
        if old_sire == new_sire and old_dam == new_dam:
            return
        self._invalidate_approved_pairings(
            old_entity["id"], "parents changed on animal %s" % old_entity["id"]
        )

    def _invalidate_approved_pairings(self, animal_id, reason):
        pairings = self.repository.list_entities(kind="pairing")
        affected = [
            pairing
            for pairing in pairings
            if pairing["status"] == "approved"
            and (
                pairing["data"].get("sire_id") == animal_id
                or pairing["data"].get("dam_id") == animal_id
            )
        ]
        for pairing in affected:
            data = dict(pairing["data"])
            data["review_reason"] = reason
            self.repository.update_entity(
                pairing["id"], pairing["version"], "pending_review", data
            )
            self.repository.create_review_task(pairing["id"], reason)
            self.audit.record(
                pairing["id"],
                SYSTEM_ACTOR,
                "review_opened",
                "approved",
                "pending_review",
                {"reason": reason, "trigger": "parent_change", "animal_id": animal_id},
            )
        if affected:
            self.process_reviews()

    # --- review processing ---------------------------------------------

    def _recalculate_pairing(self, pairing):
        sire_id = pairing["data"].get("sire_id")
        dam_id = pairing["data"].get("dam_id")
        today = self._today()
        sire_tree = self.pedigree(sire_id, today, 3)
        dam_tree = self.pedigree(dam_id, today, 3)
        coefficient = coefficient_from_trees(sire_tree, dam_tree)
        valid = coefficient <= self.REVIEW_THRESHOLD
        return {
            "valid": valid,
            "coefficient": coefficient,
            "threshold": self.REVIEW_THRESHOLD,
        }

    def process_reviews(self):
        tasks = self.repository.list_pending_review_tasks(self.MAX_REVIEW_ATTEMPTS)
        for task in tasks:
            try:
                pairing = self.repository.get_entity(task["pairing_id"])
                if not pairing:
                    self.repository.resolve_review_task(
                        task["id"], {"skipped": "pairing not found"}
                    )
                    continue
                result = self._recalculate_pairing(pairing)
                if result["valid"]:
                    snapshot = self._pedigree_snapshot(
                        pairing["data"].get("sire_id"), pairing["data"].get("dam_id")
                    )
                    data = dict(pairing["data"])
                    data["pedigree_snapshot"] = snapshot
                    data.pop("review_reason", None)
                    self.repository.update_entity(
                        pairing["id"], pairing["version"], "approved", data
                    )
                    self.repository.resolve_review_task(task["id"], result)
                    self.audit.record(
                        pairing["id"],
                        SYSTEM_ACTOR,
                        "reapprove",
                        "pending_review",
                        "approved",
                        {"trigger": "recalculation", "result": result},
                    )
                else:
                    reason = "inbreeding coefficient %.4f exceeds threshold %.3f" % (
                        result["coefficient"],
                        self.REVIEW_THRESHOLD,
                    )
                    result["reason"] = reason
                    data = dict(pairing["data"])
                    data["review_reason"] = reason
                    self.repository.update_entity(
                        pairing["id"], pairing["version"], "pending_review", data
                    )
                    self.repository.resolve_review_task(task["id"], result)
                    self.audit.record(
                        pairing["id"],
                        SYSTEM_ACTOR,
                        "review_recalculated",
                        "pending_review",
                        "pending_review",
                        {"result": result},
                    )
            except Exception as exc:
                self.repository.fail_review_task(task["id"], str(exc))
        return len(tasks)

    def pending_review(self):
        pairings = self.repository.list_entities(kind="pairing", status="pending_review")
        items = []
        for pairing in pairings:
            items.append(
                {
                    "pairing": pairing,
                    "review_reason": pairing["data"].get("review_reason"),
                    "tasks": self.repository.list_review_tasks(pairing["id"]),
                }
            )
        return items
