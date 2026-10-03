from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def parse_effective_date(value):
    """Accept YYYY-MM-DD (optionally prefixed ISO timestamp)."""
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text[:10])
    except ValueError:
        raise ValidationError("effective_date must be YYYY-MM-DD")
    return parsed.date().isoformat()


CUSTOM_CREATE = {'animal': _validate_animal}
CUSTOM_TRANSITIONS = {}

REVISABLE_FIELDS = ("name", "sire_id", "dam_id", "sex")
PARENTAGE_FIELDS = ("sire_id", "dam_id")


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active'), 'revise': (('active', 'quarantined', 'deceased'), None)}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed'), 'recheck': (('needs_review',), 'approved')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'revise': ('admin', 'registrar'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'recheck': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar')}
    INBREEDING_LIMIT = 0.125

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def parse_effective_date(self, value):
        return parse_effective_date(value)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def evaluate_pairing(self, actor, sire_view, dam_view):
        """Validate the two animals against the pairing rules using the
        revision views captured at decision time. Returns audit extras."""
        if not sire_view or not dam_view:
            raise ValidationError("pairing requires two existing animals")
        if sire_view["status"] != "active" or dam_view["status"] != "active":
            raise ValidationError("pairing animals must be active")
        coefficient = inbreeding_coefficient(sire_view["data"], dam_view["data"])
        if coefficient > self.INBREEDING_LIMIT:
            raise ValidationError("pairing exceeds inbreeding threshold")
        return {"approved_by": actor.user_id, "inbreeding": coefficient}

    def validate_revise(self, actor, entity, data):
        self._ensure_role(actor, self.ROLE_ACTIONS["revise"])
        payload = dict(data)
        if "sex" in payload and payload["sex"] not in ("male", "female", "unknown"):
            raise ValidationError("sex must be male, female or unknown")
        changes = {
            field: payload[field]
            for field in REVISABLE_FIELDS
            if field in payload and payload[field] != entity["data"].get(field)
        }
        if not changes:
            raise ValidationError("revision changes nothing")
        parentage_changed = any(field in PARENTAGE_FIELDS for field in changes)
        reason = payload.get("reason")
        if not reason:
            raise ValidationError("missing required field: reason")
        effective_date = parse_effective_date(
            payload.get("effective_date") or datetime.utcnow().date().isoformat()
        )
        return changes, effective_date, parentage_changed

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        if next_status is None:
            next_status = entity["status"]
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
