import threading
from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    Actor,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .rules import RuleEngine

SYSTEM_ACTOR = Actor("system", "coordinator")
ANCESTOR_GENERATIONS = 3


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

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
        effective_date = None
        if kind == "animal" and payload.get("effective_date"):
            effective_date = self.rules.parse_effective_date(payload["effective_date"])
        entity_id = str(payload.pop("id", "") or uuid4())
        payload.pop("effective_date", None)
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(
            entity_id, kind, status, payload, actor.user_id, effective_date=effective_date
        )
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "animal":
            if action == "revise":
                return self._revise_animal(actor, entity, data or {}, expected_version)
            return self._change_animal_status(
                actor, entity, action, data or {}, expected_version
            )
        if kind == "pairing" and action in ("approve", "recheck"):
            return self._decide_pairing(actor, entity, action, data or {}, expected_version)
        return self._generic_transition(actor, entity, action, data or {}, expected_version)

    def _change_animal_status(self, actor, entity, action, data, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated, _invalidated = self.repository.save_animal_revision(
            entity["id"],
            expected,
            next_status,
            merged,
            self._today(),
            "status action: " + action,
            actor.user_id,
            parentage_changed=False,
            invalidate_on_status_change=True,
        )
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _generic_transition(self, actor, entity, action, data, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    # ----- animal revisions -------------------------------------------------

    def _revise_animal(self, actor, entity, data, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        self.rules.validate_transition(actor, entity, "revise", dict(data), self._lookup)
        changes, effective_date, parentage_changed = self.rules.validate_revise(
            actor, entity, data
        )
        merged = dict(entity["data"])
        merged.update(changes)
        updated, invalidated = self.repository.save_animal_revision(
            entity["id"],
            expected,
            entity["status"],
            merged,
            effective_date,
            data.get("reason"),
            actor.user_id,
            parentage_changed,
        )
        self.audit.record(
            entity["id"],
            actor,
            "revise",
            entity["status"],
            updated["status"],
            {
                "changes": changes,
                "effective_date": effective_date,
                "parentage_changed": parentage_changed,
                "invalidated_pairings": invalidated,
                "reason": data.get("reason"),
            },
        )
        return updated

    def revisions(self, animal_id):
        entity = self.repository.get_entity(animal_id)
        if not entity or entity["kind"] != "animal":
            raise NotFoundError("animal not found: " + animal_id)
        return self.repository.list_revisions(animal_id)

    # ----- pedigree as of a date -------------------------------------------

    def pedigree_as_of(self, animal_id, on_date):
        entity = self.repository.get_entity(animal_id)
        if not entity or entity["kind"] != "animal":
            raise NotFoundError("animal not found: " + animal_id)
        root = self.repository.get_revision_at(animal_id, on_date)
        if not root:
            return {
                "id": animal_id,
                "as_of_date": on_date,
                "root": None,
                "message": "no registered revision effective on this date",
            }
        return {
            "id": animal_id,
            "as_of_date": on_date,
            "root": self._build_ancestor_tree(root, on_date, ANCESTOR_GENERATIONS, ()),
        }

    def _build_ancestor_tree(self, view, on_date, depth, trail):
        node = {
            "id": view["id"],
            "version": view["version"],
            "status": view["status"],
            "effective_date": view["effective_date"],
            "data": view["data"],
        }
        if depth <= 0:
            node["sire"] = None
            node["dam"] = None
            return node
        next_trail = trail + (view["id"],)
        node["sire"] = self._parent_node(
            view["data"].get("sire_id"), on_date, depth - 1, next_trail
        )
        node["dam"] = self._parent_node(
            view["data"].get("dam_id"), on_date, depth - 1, next_trail
        )
        return node

    def _parent_node(self, parent_id, on_date, depth, trail):
        if not parent_id:
            return None
        if parent_id in trail:
            return {"id": parent_id, "cycle": True}
        parent_view = self.repository.get_revision_at(parent_id, on_date)
        if not parent_view:
            return {
                "id": parent_id,
                "missing": True,
                "message": "no registered revision effective on this date",
            }
        return self._build_ancestor_tree(parent_view, on_date, depth, trail)

    # ----- pairing decisions and approval snapshots -------------------------

    def _animal_view_at(self, animal_id, on_date):
        entity = self.repository.get_entity(animal_id)
        if not entity or entity["kind"] != "animal":
            raise ValidationError("animal not found: " + str(animal_id))
        view = self.repository.get_revision_at(animal_id, on_date)
        if not view:
            raise ValidationError(
                "animal has no registered revision effective on %s: %s"
                % (on_date, animal_id)
            )
        return view

    def _decide_pairing(self, actor, entity, action, data, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)

        prior = self.repository.get_latest_approval(entity["id"])
        if action == "recheck" and prior:
            merged.setdefault("sire_id", prior["sire_id"])
            merged.setdefault("dam_id", prior["dam_id"])
            merged.setdefault("approvals", entity["data"].get("approvals"))

        on_date = self._today()
        sire_view = self._animal_view_at(merged.get("sire_id"), on_date)
        dam_view = self._animal_view_at(merged.get("dam_id"), on_date)
        extras = self.rules.evaluate_pairing(actor, sire_view, dam_view)
        merged.update(extras)

        approval = {
            "sire_id": sire_view["id"],
            "dam_id": dam_view["id"],
            "sire_version": sire_view["version"],
            "dam_version": dam_view["version"],
            "sire_snapshot": sire_view,
            "dam_snapshot": dam_view,
            "pedigree": {
                "sire": self._build_ancestor_tree(sire_view, on_date, ANCESTOR_GENERATIONS, ()),
                "dam": self._build_ancestor_tree(dam_view, on_date, ANCESTOR_GENERATIONS, ()),
            },
            "inbreeding": extras["inbreeding"],
            "note": "manual recheck" if action == "recheck" else None,
        }
        updated = self.repository.approve_pairing(
            entity["id"], expected, next_status, merged, approval, actor.user_id
        )
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {
                "sire_id": sire_view["id"],
                "dam_id": dam_view["id"],
                "sire_version": sire_view["version"],
                "dam_version": dam_view["version"],
                "inbreeding": extras["inbreeding"],
            },
        )
        return updated

    def approvals(self, pairing_id):
        entity = self.repository.get_entity(pairing_id)
        if not entity or entity["kind"] != "pairing":
            raise NotFoundError("pairing not found: " + pairing_id)
        return self.repository.list_approvals(pairing_id)

    # ----- review queue and recalculation -----------------------------------

    def pending_reviews(self):
        items = self.repository.list_pending_reviews()
        for item in items:
            item["pairing"] = self.repository.get_entity(item["pairing_id"])
        return items

    def retry_review(self, actor, pairing_id):
        self.rules._ensure_role(actor, ("admin", "coordinator"))
        pairing = self.repository.get_entity(pairing_id)
        if not pairing or pairing["kind"] != "pairing":
            raise NotFoundError("pairing not found: " + pairing_id)
        if pairing["status"] != "needs_review":
            raise ConflictError("pairing is not awaiting review: " + pairing_id)
        claimed = self.repository.claim_review(pairing_id)
        if not claimed:
            raise ConflictError("review item is not in a retryable state: " + pairing_id)
        result = self._process_review_item(claimed)
        if result is not None:
            return result
        # rule still fails: return the kept pending-review item with the reason
        return self.repository.get_review(pairing_id)

    def process_due_reviews(self, limit=5):
        """One sweep of the queue. Returns processed pairing ids."""
        processed = []
        for item in self.repository.claim_due_reviews(limit=limit):
            result = self._process_review_item(item)
            if result is not None:
                processed.append(result["id"])
        return processed

    def _process_review_item(self, item):
        pairing_id = item["pairing_id"]
        pairing = self.repository.get_entity(pairing_id)
        if not pairing:
            self.repository.block_review(pairing_id, "pairing entity no longer exists")
            return None
        if pairing["status"] != "needs_review":
            self.repository.complete_review(pairing_id)
            return pairing
        prior = self.repository.get_latest_approval(pairing_id)
        try:
            sire_id = pairing["data"].get("sire_id")
            dam_id = pairing["data"].get("dam_id")
            if prior:
                sire_id = sire_id or prior["sire_id"]
                dam_id = dam_id or prior["dam_id"]
            on_date = self._today()
            sire_view = self._animal_view_at(sire_id, on_date)
            dam_view = self._animal_view_at(dam_id, on_date)
            extras = self.rules.evaluate_pairing(SYSTEM_ACTOR, sire_view, dam_view)
        except ValidationError as exc:
            # Rule-level failure: keep the item for manual re-check after data fixes.
            self.repository.block_review(pairing_id, str(exc))
            self.audit.record(
                pairing_id, SYSTEM_ACTOR, "review_blocked",
                "needs_review", "needs_review", {"error": str(exc)},
            )
            return None
        except Exception as exc:
            # Technical failure: the item stays pending and the sweep retries it.
            self.repository.fail_review(pairing_id, str(exc))
            return None

        merged = dict(pairing["data"])
        merged.update(extras)
        merged["sire_id"] = sire_id
        merged["dam_id"] = dam_id
        approval = {
            "sire_id": sire_view["id"],
            "dam_id": dam_view["id"],
            "sire_version": sire_view["version"],
            "dam_version": dam_view["version"],
            "sire_snapshot": sire_view,
            "dam_snapshot": dam_view,
            "pedigree": {
                "sire": self._build_ancestor_tree(sire_view, on_date, ANCESTOR_GENERATIONS, ()),
                "dam": self._build_ancestor_tree(dam_view, on_date, ANCESTOR_GENERATIONS, ()),
            },
            "inbreeding": extras["inbreeding"],
            "note": "automatically re-approved after recalculation",
        }
        try:
            updated = self.repository.approve_pairing(
                pairing_id, pairing["version"], "approved", merged, approval,
                SYSTEM_ACTOR.user_id,
            )
            self.audit.record(
                pairing_id, SYSTEM_ACTOR, "review_approved",
                "needs_review", "approved",
                {
                    "sire_version": sire_view["version"],
                    "dam_version": dam_view["version"],
                    "inbreeding": extras["inbreeding"],
                    "attempts": item.get("attempts"),
                },
            )
            return updated
        except Exception as exc:
            # Transient failure (version race, database hiccup): retry later.
            self.repository.fail_review(pairing_id, str(exc))
            return None

    # ----- reads ------------------------------------------------------------

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


class ReviewWorker:
    """Background sweeper: retries failed recalculations and resumes after restart."""

    def __init__(self, service, interval=1.0, batch=5):
        self.service = service
        self.interval = interval
        self.batch = batch
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.service.repository.recover_stale_reviews()
        self._thread = threading.Thread(target=self._run, name="review-worker", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.service.process_due_reviews(limit=self.batch)
            except Exception:
                # Never let the sweep die; the next tick retries everything.
                pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
