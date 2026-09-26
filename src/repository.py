"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS patients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS helicopters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hospitals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_patients_state ON patients(state);
                CREATE INDEX IF NOT EXISTS idx_helicopters_state ON helicopters(state);
                CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 空地转运：伤员 / 直升机 / 医院 ----

    ENTITY_TABLES = {"patient": "patients", "helicopter": "helicopters", "hospital": "hospitals"}

    def _entity_row(self, table: str, entity_id: int, connection: sqlite3.Connection = None) -> sqlite3.Row:
        own = connection is None
        if own:
            connection = self._connect().__enter__()
        try:
            row = connection.execute("SELECT * FROM %s WHERE id=?" % table, (entity_id,)).fetchone()
        finally:
            if own:
                connection.__exit__(None, None, None)
        if row is None:
            raise NotFound("资源不存在")
        return row

    def entity_get(self, entity_type: str, entity_id: int) -> Dict[str, Any]:
        table = self.ENTITY_TABLES.get(entity_type)
        if table is None:
            raise NotFound("未知资源类型")
        with self._connect() as connection:
            row = self._entity_row(table, entity_id, connection)
        return self._row(row)

    def entity_create(self, entity_type: str, code: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        table = self.ENTITY_TABLES[entity_type]
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO %s(code,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)" % table,
                    (code, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                entity_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO events(entity_type,entity_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                    (entity_type, entity_id, "registered", actor_id, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM %s WHERE id=?" % table, (entity_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("编号已存在") from exc
        return self._row(row)

    def entity_list(self, entity_type: str, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        table = self.ENTITY_TABLES[entity_type]
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM %s WHERE state=? ORDER BY id DESC LIMIT ?" % table, (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM %s ORDER BY id DESC LIMIT ?" % table, (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def atomic_batch(
        self,
        changes: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """单事务内更新多个实体并追加事件；按版本乐观锁，全部成功或全部回滚。"""
        now = _now()
        result: Dict[str, List[Dict[str, Any]]] = {}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for change in changes:
                    table = self.ENTITY_TABLES[change["entity_type"]]
                    row = connection.execute("SELECT version FROM %s WHERE id=?" % table, (change["entity_id"],)).fetchone()
                    if row is None:
                        raise NotFound("资源不存在")
                    if int(row["version"]) != int(change["expected_version"]):
                        raise Conflict("版本冲突，请刷新后重试")
                    version = int(change["expected_version"]) + 1
                    connection.execute(
                        "UPDATE %s SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?" % table,
                        (
                            change["state"],
                            version,
                            json.dumps(change["payload"], ensure_ascii=False, sort_keys=True),
                            change["actor_id"],
                            now,
                            change["entity_id"],
                        ),
                    )
                for event in events:
                    connection.execute(
                        "INSERT INTO events(entity_type,entity_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            event["entity_type"],
                            event["entity_id"],
                            event["action"],
                            event["actor_id"],
                            json.dumps(event.get("details", {}), ensure_ascii=False, sort_keys=True),
                            now,
                        ),
                    )
                for change in changes:
                    table = self.ENTITY_TABLES[change["entity_type"]]
                    row = connection.execute("SELECT * FROM %s WHERE id=?" % table, (change["entity_id"],)).fetchone()
                    result.setdefault(change["entity_type"], []).append(self._row(row))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return result

    def add_event(self, entity_type: str, entity_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        table = self.ENTITY_TABLES[entity_type]
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM %s WHERE id=?" % table, (entity_id,)).fetchone()
            if row is None:
                raise NotFound("资源不存在")
            connection.execute(
                "INSERT INTO events(entity_type,entity_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                (entity_type, entity_id, action, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def entity_timeline(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        self.entity_get(entity_type, entity_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity_type, entity_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def air_stats(self) -> Dict[str, Dict[str, int]]:
        stats: Dict[str, Dict[str, int]] = {}
        with self._connect() as connection:
            for entity_type, table in self.ENTITY_TABLES.items():
                rows = connection.execute("SELECT state, COUNT(*) AS total FROM %s GROUP BY state" % table).fetchall()
                stats[entity_type] = {str(row["state"]): int(row["total"]) for row in rows}
        return stats
