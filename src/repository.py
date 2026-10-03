import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
                CREATE TABLE IF NOT EXISTS entity_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    effective_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(entity_id, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_revisions_entity_date
                    ON entity_revisions(entity_id, effective_date);
                CREATE TABLE IF NOT EXISTS review_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pairing_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_review_tasks_status
                    ON review_tasks(status);
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
            """)

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

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
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
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
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

    # --- revisions -------------------------------------------------------

    @staticmethod
    def _revision_from_row(row):
        return {
            "entity_id": row["entity_id"],
            "revision": int(row["revision"]),
            "effective_date": row["effective_date"],
            "status": row["status"],
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def add_revision(self, entity_id, revision, effective_date, status, data, actor_id):
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entity_revisions(entity_id, revision, effective_date, status, data, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entity_id, revision, effective_date, status, payload, actor_id, now),
            )
        return self.get_revision(entity_id, revision)

    def get_revision(self, entity_id, revision):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entity_revisions WHERE entity_id = ? AND revision = ?",
                (entity_id, revision),
            ).fetchone()
        return self._revision_from_row(row) if row else None

    def get_revision_at(self, entity_id, date):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entity_revisions WHERE entity_id = ? AND effective_date <= ? "
                "ORDER BY effective_date DESC, revision DESC LIMIT 1",
                (entity_id, str(date)),
            ).fetchone()
        return self._revision_from_row(row) if row else None

    def list_revisions(self, entity_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entity_revisions WHERE entity_id = ? ORDER BY revision",
                (entity_id,),
            ).fetchall()
        return [self._revision_from_row(row) for row in rows]

    def latest_revision(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entity_revisions WHERE entity_id = ? ORDER BY revision DESC LIMIT 1",
                (entity_id,),
            ).fetchone()
        return self._revision_from_row(row) if row else None

    def ensure_baseline_revisions(self):
        """Idempotently seed a starting revision for animals that predate revisions."""
        with self._connect() as connection:
            animals = connection.execute(
                "SELECT * FROM entities WHERE kind = 'animal'"
            ).fetchall()
            for row in animals:
                count = connection.execute(
                    "SELECT COUNT(*) FROM entity_revisions WHERE entity_id = ?",
                    (row["id"],),
                ).fetchone()[0]
                if count:
                    continue
                effective_date = (row["created_at"] or "")[:10] or utcnow()[:10]
                connection.execute(
                    "INSERT INTO entity_revisions(entity_id, revision, effective_date, status, data, created_by, created_at) "
                    "VALUES (?, 1, ?, ?, ?, ?, ?)",
                    (row["id"], effective_date, row["status"], row["data"], row["created_by"], row["created_at"]),
                )

    # --- review tasks ---------------------------------------------------

    @staticmethod
    def _review_task_from_row(row):
        return {
            "id": row["id"],
            "pairing_id": row["pairing_id"],
            "reason": row["reason"],
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_review_task(self, pairing_id, reason):
        now = utcnow()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO review_tasks(pairing_id, reason, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, 'pending', 0, ?, ?)",
                (pairing_id, reason, now, now),
            )
            return cursor.lastrowid

    def list_pending_review_tasks(self, max_attempts=5):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_tasks WHERE status IN ('pending', 'failed') AND attempts < ? ORDER BY id",
                (max_attempts,),
            ).fetchall()
        return [self._review_task_from_row(row) for row in rows]

    def resolve_review_task(self, task_id, result):
        with self._connect() as connection:
            connection.execute(
                "UPDATE review_tasks SET status = 'resolved', result = ?, updated_at = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), utcnow(), task_id),
            )

    def fail_review_task(self, task_id, error):
        with self._connect() as connection:
            connection.execute(
                "UPDATE review_tasks SET status = 'failed', attempts = attempts + 1, last_error = ?, updated_at = ? WHERE id = ?",
                (str(error), utcnow(), task_id),
            )

    def list_review_tasks(self, pairing_id=None):
        with self._connect() as connection:
            if pairing_id:
                rows = connection.execute(
                    "SELECT * FROM review_tasks WHERE pairing_id = ? ORDER BY id",
                    (pairing_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM review_tasks ORDER BY id"
                ).fetchall()
        return [self._review_task_from_row(row) for row in rows]

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
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
