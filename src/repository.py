import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS animal_revisions (
                    entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    effective_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(entity_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_animal_revisions_date
                    ON animal_revisions(entity_id, effective_date, version);
                CREATE TABLE IF NOT EXISTS pairing_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pairing_id TEXT NOT NULL,
                    approval_seq INTEGER NOT NULL,
                    sire_id TEXT NOT NULL,
                    dam_id TEXT NOT NULL,
                    sire_version INTEGER NOT NULL,
                    dam_version INTEGER NOT NULL,
                    sire_snapshot TEXT NOT NULL,
                    dam_snapshot TEXT NOT NULL,
                    pedigree TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    inbreeding REAL,
                    note TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    superseded_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pairing_approvals_pairing
                    ON pairing_approvals(pairing_id, approval_seq);
                CREATE INDEX IF NOT EXISTS idx_pairing_approvals_active
                    ON pairing_approvals(active, sire_id);
                CREATE INDEX IF NOT EXISTS idx_pairing_approvals_active_dam
                    ON pairing_approvals(active, dam_id);
                CREATE TABLE IF NOT EXISTS review_queue (
                    pairing_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT
                );
            """)
            self._migrate_baseline_revisions(connection)
            self._migrate_baseline_approvals(connection)

    def _migrate_baseline_revisions(self, connection):
        """Animals written before revisions existed get one starting revision."""
        connection.execute(
            "INSERT INTO animal_revisions("
            "entity_id, version, effective_date, status, data, reason, created_by, created_at) "
            "SELECT id, version, substr(created_at, 1, 10), status, data, "
            "'baseline: imported from pre-revision data', created_by, created_at "
            "FROM entities WHERE kind = 'animal' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM animal_revisions ar WHERE ar.entity_id = entities.id)"
        )

    def _migrate_baseline_approvals(self, connection):
        """Approved/completed pairings written before approval history existed."""
        rows = connection.execute(
            "SELECT * FROM entities WHERE kind = 'pairing' AND status IN ('approved', 'completed')"
        ).fetchall()
        for row in rows:
            exists = connection.execute(
                "SELECT 1 FROM pairing_approvals WHERE pairing_id = ?", (row["id"],)
            ).fetchone()
            if exists:
                continue
            data = json.loads(row["data"])
            now = utcnow()

            def snapshot(parent_id):
                if not parent_id:
                    return 0, {"id": parent_id, "missing": True}
                parent = connection.execute(
                    "SELECT * FROM entities WHERE id = ?", (parent_id,)
                ).fetchone()
                if not parent:
                    return 0, {"id": parent_id, "missing": True}
                view = self._entity_from_row(parent)
                return view["version"], self._snapshot_view(view, now[:10])

            sire_version, sire_snapshot = snapshot(data.get("sire_id"))
            dam_version, dam_snapshot = snapshot(data.get("dam_id"))
            connection.execute(
                "INSERT INTO pairing_approvals("
                "pairing_id, approval_seq, sire_id, dam_id, sire_version, dam_version, "
                "sire_snapshot, dam_snapshot, pedigree, decision, inbreeding, note, "
                "active, superseded_reason, created_by, created_at) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 'approved', NULL, "
                "'baseline: reconstructed from current data', 1, NULL, ?, ?)",
                (
                    row["id"],
                    data.get("sire_id"),
                    data.get("dam_id"),
                    sire_version,
                    dam_version,
                    _dump(sire_snapshot),
                    _dump(dam_snapshot),
                    _dump({}),
                    row["created_by"],
                    now,
                ),
            )

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _revision_from_row(row):
        return {
            "id": row["entity_id"],
            "kind": "animal",
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "effective_date": row["effective_date"],
            "reason": row["reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _snapshot_view(view, effective_date):
        return {
            "id": view["id"],
            "version": view["version"],
            "status": view["status"],
            "effective_date": effective_date,
            "data": view["data"],
        }

    @staticmethod
    def _insert_audit(connection, entity_id, actor_id, actor_role, action,
                      from_status, to_status, detail, created_at=None):
        stamp = created_at or utcnow()
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
            "from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                _dump(detail or {}),
                stamp,
            ),
        )

    def create_entity(self, entity_id, kind, status, data, actor_id, effective_date=None):
        now = utcnow()
        payload = _dump(data)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
            if kind == "animal":
                connection.execute(
                    "INSERT INTO animal_revisions("
                    "entity_id, version, effective_date, status, data, reason, created_by, created_at) "
                    "VALUES (?, 1, ?, ?, ?, 'registered', ?, ?)",
                    (entity_id, effective_date or now[:10], status, payload, actor_id, now),
                )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = _dump(data)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def save_animal_revision(self, entity_id, expected_version, status, data,
                             effective_date, reason, actor_id, parentage_changed,
                             invalidate_on_status_change=False):
        """Insert a new animal revision and atomically invalidate pairings whose
        approved basis changed. Returns (updated_entity, invalidated_pairing_ids).

        Parentage edits always invalidate; a status action (quarantine, death)
        also invalidates because approval requires both animals to be active."""
        now = utcnow()
        payload = _dump(data)
        connection = self._connect()
        invalidated = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version, status FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            status_changed = invalidate_on_status_change and row["status"] != status
            next_version = current_version + 1
            connection.execute(
                "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? "
                "WHERE id = ?",
                (status, next_version, payload, now, entity_id),
            )
            connection.execute(
                "INSERT INTO animal_revisions("
                "entity_id, version, effective_date, status, data, reason, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entity_id, next_version, effective_date, status, payload,
                 reason, actor_id, now),
            )
            if parentage_changed or status_changed:
                approval_rows = connection.execute(
                    "SELECT pa.* FROM pairing_approvals pa "
                    "JOIN entities e ON e.id = pa.pairing_id "
                    "WHERE pa.active = 1 AND e.kind = 'pairing' AND e.status = 'approved' "
                    "AND (pa.sire_id = ? OR pa.dam_id = ?)",
                    (entity_id, entity_id),
                ).fetchall()
                for approval_row in approval_rows:
                    pairing_id = approval_row["pairing_id"]
                    if approval_row["sire_id"] == entity_id:
                        role_name, recorded_version = "sire", approval_row["sire_version"]
                    else:
                        role_name, recorded_version = "dam", approval_row["dam_version"]
                    if parentage_changed:
                        invalidate_reason = (
                            "parentage of %s %s changed after approval: "
                            "decision recorded against version %d, current version is %d"
                            % (role_name, entity_id, int(recorded_version), next_version)
                        )
                    else:
                        invalidate_reason = (
                            "%s %s status changed to %s after approval: "
                            "decision recorded against version %d, current version is %d"
                            % (role_name, entity_id, status,
                               int(recorded_version), next_version)
                        )
                    connection.execute(
                        "UPDATE pairing_approvals SET active = 0, superseded_reason = ? "
                        "WHERE id = ?",
                        (invalidate_reason, approval_row["id"]),
                    )
                    connection.execute(
                        "UPDATE entities SET status = 'needs_review', "
                        "version = version + 1, updated_at = ? "
                        "WHERE id = ? AND status = 'approved'",
                        (now, pairing_id),
                    )
                    self._insert_audit(
                        connection,
                        pairing_id,
                        "system",
                        "coordinator",
                        "auto_needs_review",
                        "approved",
                        "needs_review",
                        {"reason": invalidate_reason, "trigger_animal": entity_id},
                        created_at=now,
                    )
                    connection.execute(
                        "INSERT INTO review_queue("
                        "pairing_id, status, reason, attempts, last_error, "
                        "created_at, updated_at, claimed_at) "
                        "VALUES (?, 'pending', ?, 0, NULL, ?, ?, NULL) "
                        "ON CONFLICT(pairing_id) DO UPDATE SET "
                        "status = 'pending', reason = excluded.reason, "
                        "last_error = NULL, claimed_at = NULL, updated_at = excluded.updated_at",
                        (pairing_id, invalidate_reason, now, now),
                    )
                    invalidated.append(pairing_id)
            # A new revision on this animal may fix the data behind an earlier
            # rule failure: put related blocked items back on the pending sweep.
            connection.execute(
                "UPDATE review_queue SET status = 'pending', last_error = NULL, "
                "claimed_at = NULL, updated_at = ? "
                "WHERE status = 'blocked' AND pairing_id IN ("
                "SELECT e.id FROM entities e WHERE e.kind = 'pairing' AND ("
                "json_extract(e.data, '$.sire_id') = ? "
                "OR json_extract(e.data, '$.dam_id') = ?))",
                (now, entity_id, entity_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id), invalidated

    def list_revisions(self, entity_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM animal_revisions WHERE entity_id = ? ORDER BY version",
                (entity_id,),
            ).fetchall()
        return [self._revision_from_row(row) for row in rows]

    def get_revision_at(self, entity_id, effective_date):
        """Revision that was in effect on the given date (inclusive).

        Among revisions effective by that day, the highest version wins, so a
        later back-dated correction correctly overrides earlier knowledge."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM animal_revisions WHERE entity_id = ? AND effective_date <= ? "
                "ORDER BY version DESC LIMIT 1",
                (entity_id, effective_date),
            ).fetchone()
        return self._revision_from_row(row) if row else None

    def approve_pairing(self, pairing_id, expected_version, status, data,
                        approval, actor_id):
        """Approve/re-approve a pairing in one transaction: bump the entity,
        record versions+snapshots of both animals, and resolve the review item."""
        now = utcnow()
        payload = _dump(data)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version, status FROM entities WHERE id = ?", (pairing_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + pairing_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            next_version = current_version + 1
            connection.execute(
                "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? "
                "WHERE id = ?",
                (status, next_version, payload, now, pairing_id),
            )
            seq_row = connection.execute(
                "SELECT COALESCE(MAX(approval_seq), 0) + 1 AS next_seq "
                "FROM pairing_approvals WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO pairing_approvals("
                "pairing_id, approval_seq, sire_id, dam_id, sire_version, dam_version, "
                "sire_snapshot, dam_snapshot, pedigree, decision, inbreeding, note, "
                "active, superseded_reason, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?, 1, NULL, ?, ?)",
                (
                    pairing_id,
                    int(seq_row["next_seq"]),
                    approval["sire_id"],
                    approval["dam_id"],
                    approval["sire_version"],
                    approval["dam_version"],
                    _dump(approval["sire_snapshot"]),
                    _dump(approval["dam_snapshot"]),
                    _dump(approval["pedigree"]),
                    approval.get("inbreeding"),
                    approval.get("note"),
                    actor_id,
                    now,
                ),
            )
            connection.execute(
                "UPDATE review_queue SET status = 'done', last_error = NULL, "
                "claimed_at = NULL, updated_at = ? WHERE pairing_id = ? AND status != 'done'",
                (now, pairing_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(pairing_id)

    @staticmethod
    def _approval_from_row(row):
        return {
            "id": row["id"],
            "pairing_id": row["pairing_id"],
            "approval_seq": int(row["approval_seq"]),
            "sire_id": row["sire_id"],
            "dam_id": row["dam_id"],
            "sire_version": int(row["sire_version"]),
            "dam_version": int(row["dam_version"]),
            "sire_snapshot": json.loads(row["sire_snapshot"]),
            "dam_snapshot": json.loads(row["dam_snapshot"]),
            "pedigree": json.loads(row["pedigree"]),
            "decision": row["decision"],
            "inbreeding": row["inbreeding"],
            "note": row["note"],
            "active": bool(row["active"]),
            "superseded_reason": row["superseded_reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def list_approvals(self, pairing_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pairing_approvals WHERE pairing_id = ? ORDER BY approval_seq",
                (pairing_id,),
            ).fetchall()
        return [self._approval_from_row(row) for row in rows]

    def get_latest_approval(self, pairing_id, active_only=False):
        sql = "SELECT * FROM pairing_approvals WHERE pairing_id = ?"
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY approval_seq DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(sql, (pairing_id,)).fetchone()
        return self._approval_from_row(row) if row else None

    def recover_stale_reviews(self):
        """Re-queue items left 'processing' by a crashed previous process."""
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE review_queue SET status = 'pending', claimed_at = NULL, "
                "updated_at = ? WHERE status = 'processing'",
                (now,),
            )

    def claim_due_reviews(self, limit=5):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM review_queue WHERE status = 'pending' "
                "ORDER BY updated_at, pairing_id LIMIT ?",
                (int(limit),),
            ).fetchall()
            claimed = []
            for row in rows:
                connection.execute(
                    "UPDATE review_queue SET status = 'processing', "
                    "attempts = attempts + 1, claimed_at = ?, updated_at = ? "
                    "WHERE pairing_id = ? AND status = 'pending'",
                    (now, now, row["pairing_id"]),
                )
                claimed.append(self._review_from_row(row, now_override=now))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return claimed

    @staticmethod
    def _review_from_row(row, now_override=None):
        return {
            "pairing_id": row["pairing_id"],
            "status": row["status"],
            "reason": row["reason"],
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "claimed_at": row["claimed_at"] or now_override,
        }

    def claim_review(self, pairing_id):
        """Manual retry: claim one item from pending or blocked atomically."""
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_queue WHERE pairing_id = ?", (pairing_id,)
            ).fetchone()
            if not row or row["status"] not in ("pending", "blocked"):
                connection.commit()
                return None
            connection.execute(
                "UPDATE review_queue SET status = 'processing', "
                "attempts = attempts + 1, claimed_at = ?, updated_at = ? "
                "WHERE pairing_id = ? AND status IN ('pending', 'blocked')",
                (now, now, pairing_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        item = self._review_from_row(row)
        item["status"] = "processing"
        item["attempts"] = item["attempts"] + 1
        item["claimed_at"] = now
        return item

    def _set_review_status(self, pairing_id, status, error=None):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE review_queue SET status = ?, last_error = ?, "
                "claimed_at = NULL, updated_at = ? WHERE pairing_id = ?",
                (status, error, now, pairing_id),
            )

    def complete_review(self, pairing_id):
        self._set_review_status(pairing_id, "done")

    def fail_review(self, pairing_id, error):
        """Transient failure: item stays in the queue and will be retried."""
        self._set_review_status(pairing_id, "pending", error=error)

    def block_review(self, pairing_id, error):
        """Rule-level failure: kept for manual re-check after data is fixed."""
        self._set_review_status(pairing_id, "blocked", error=error)

    def requeue_review(self, pairing_id):
        now = utcnow()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE review_queue SET status = 'pending', claimed_at = NULL, "
                "updated_at = ? WHERE pairing_id = ? AND status IN ('blocked', 'pending')",
                (now, pairing_id),
            )
        return cursor.rowcount > 0

    def get_review(self, pairing_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_queue WHERE pairing_id = ?", (pairing_id,)
            ).fetchone()
        return self._review_from_row(row) if row else None

    def list_pending_reviews(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_queue WHERE status != 'done' "
                "ORDER BY CASE status WHEN 'processing' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
                "updated_at, pairing_id"
            ).fetchall()
        return [self._review_from_row(row) for row in rows]

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            self._insert_audit(
                connection, entity_id, actor_id, actor_role, action,
                from_status, to_status, detail,
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
