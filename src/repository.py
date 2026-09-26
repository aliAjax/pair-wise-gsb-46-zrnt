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
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    mission_id INTEGER,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS aircraft (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    aircraft_code TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    locked_mission_id INTEGER,
                    locked_slot TEXT,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hospitals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    beds_total INTEGER NOT NULL,
                    beds_reserved INTEGER NOT NULL DEFAULT 0,
                    beds_used INTEGER NOT NULL DEFAULT 0,
                    slots_total INTEGER NOT NULL,
                    slots_reserved INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS missions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    patient_id INTEGER NOT NULL,
                    aircraft_id INTEGER NOT NULL,
                    hospital_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS air_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_patients_state ON patients(state);
                CREATE INDEX IF NOT EXISTS idx_aircraft_status ON aircraft(status);
                CREATE INDEX IF NOT EXISTS idx_missions_state ON missions(state);
                CREATE INDEX IF NOT EXISTS idx_air_audit_target ON air_audit_events(scope, target_id, id);
                """
            )

    # ---- 地面调度（原有） ----
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

    # ---- 空地转运 ----
    def transaction(self) -> "AirTransaction":
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return AirTransaction(connection)

    @staticmethod
    def _patient_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _aircraft_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _hospital_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["beds_available"] = int(item["beds_total"]) - int(item["beds_reserved"]) - int(item["beds_used"])
        item["slots_available"] = int(item["slots_total"]) - int(item["slots_reserved"])
        return item

    @staticmethod
    def _mission_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _audit_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item

    def get_patient(self, patient_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        if row is None:
            raise NotFound("伤员不存在")
        return self._patient_row(row)

    def get_aircraft(self, aircraft_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM aircraft WHERE id=?", (aircraft_id,)).fetchone()
        if row is None:
            raise NotFound("直升机不存在")
        return self._aircraft_row(row)

    def get_hospital(self, hospital_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
        if row is None:
            raise NotFound("医院不存在")
        return self._hospital_row(row)

    def get_mission(self, mission_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            raise NotFound("转运任务不存在")
        return self._mission_row(row)

    def _list(self, table: str, mapper: str, state: Optional[str], limit: int) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        mapping = {
            "patients": (self._patient_row, "patients"),
            "aircraft": (self._aircraft_row, "aircraft"),
            "hospitals": (self._hospital_row, "hospitals"),
            "missions": (self._mission_row, "missions"),
        }
        row_mapper, real_table = mapping[table]
        with self._connect() as connection:
            if state:
                rows = connection.execute(
                    "SELECT * FROM %s WHERE state=? ORDER BY id DESC LIMIT ?" % real_table, (state, limit)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM %s ORDER BY id DESC LIMIT ?" % real_table, (limit,)).fetchall()
        return [row_mapper(row) for row in rows]

    def list_patients(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        return self._list("patients", "_patient_row", state, limit)

    def list_aircraft(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM aircraft WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM aircraft ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._aircraft_row(row) for row in rows]

    def list_hospitals(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM hospitals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._hospital_row(row) for row in rows]

    def list_missions(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        return self._list("missions", "_mission_row", state, limit)

    def air_timeline(self, scope: str, target_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM air_audit_events WHERE scope=? AND target_id=? ORDER BY id", (scope, target_id)
            ).fetchall()
        return [self._audit_row(row) for row in rows]

    def air_stats(self) -> Dict[str, Any]:
        with self._connect() as connection:
            patient_rows = connection.execute("SELECT state, COUNT(*) AS total FROM patients GROUP BY state").fetchall()
            mission_rows = connection.execute("SELECT state, COUNT(*) AS total FROM missions GROUP BY state").fetchall()
            aircraft_rows = connection.execute("SELECT status, COUNT(*) AS total FROM aircraft GROUP BY status").fetchall()
            hospital_rows = connection.execute(
                "SELECT name, beds_total, beds_reserved, beds_used, slots_total, slots_reserved FROM hospitals ORDER BY id"
            ).fetchall()
        return {
            "patients": {str(row["state"]): int(row["total"]) for row in patient_rows},
            "missions": {str(row["state"]): int(row["total"]) for row in mission_rows},
            "aircraft": {str(row["status"]): int(row["total"]) for row in aircraft_rows},
            "hospitals": [
                {
                    "name": row["name"],
                    "beds_total": int(row["beds_total"]),
                    "beds_available": int(row["beds_total"]) - int(row["beds_reserved"]) - int(row["beds_used"]),
                    "slots_total": int(row["slots_total"]),
                    "slots_available": int(row["slots_total"]) - int(row["slots_reserved"]),
                }
                for row in hospital_rows
            ],
        }


class AirTransaction:
    """空地转运写操作事务：跨伤员/直升机/医院/任务的改动在同一事务内提交。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._closed = False

    def __enter__(self) -> "AirTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    def commit(self) -> None:
        if not self._closed:
            self.connection.commit()
            self.connection.close()
            self._closed = True

    def rollback(self) -> None:
        if not self._closed:
            self.connection.rollback()
            self.connection.close()
            self._closed = True

    # ---- 事务内读取（带行锁语义，BEGIN IMMEDIATE 下为串行写） ----
    def _must_exist(self, table: str, row_id: int, message: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM %s WHERE id=?" % table, (row_id,)).fetchone()
        if row is None:
            raise NotFound(message)
        return row

    def load_patient(self, patient_id: int) -> Dict[str, Any]:
        return Repository._patient_row(self._must_exist("patients", patient_id, "伤员不存在"))

    def load_aircraft(self, aircraft_id: int) -> Dict[str, Any]:
        return Repository._aircraft_row(self._must_exist("aircraft", aircraft_id, "直升机不存在"))

    def load_hospital(self, hospital_id: int) -> Dict[str, Any]:
        return Repository._hospital_row(self._must_exist("hospitals", hospital_id, "医院不存在"))

    def load_mission(self, mission_id: int) -> Dict[str, Any]:
        return Repository._mission_row(self._must_exist("missions", mission_id, "转运任务不存在"))

    # ---- 登记写入 ----
    def insert_patient(self, reference: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        return self._insert(
            "patients",
            "reference",
            "伤员编号已存在",
            (reference, "registered", None, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id),
            "reference,state,mission_id,payload,created_by,updated_by",
            Repository._patient_row,
        )

    def insert_aircraft(self, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        return self._insert(
            "aircraft",
            "aircraft_code",
            "直升机编号已存在",
            (payload["aircraft_code"], "available", None, None, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id),
            "aircraft_code,status,locked_mission_id,locked_slot,payload,created_by,updated_by",
            Repository._aircraft_row,
        )

    def insert_hospital(self, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        return self._insert(
            "hospitals",
            "name",
            "医院已登记",
            (payload["name"], payload["beds_total"], 0, 0, payload["helipad_slots_total"], 0, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id),
            "name,beds_total,beds_reserved,beds_used,slots_total,slots_reserved,payload,created_by,updated_by",
            Repository._hospital_row,
        )

    def _insert(self, table: str, _conflict_col: str, conflict_message: str, values: tuple, columns: str, mapper) -> Dict[str, Any]:
        placeholders = ",".join("?" for _ in values)
        now = _now()
        try:
            cursor = self.connection.execute(
                "INSERT INTO %s(%s,created_at,updated_at) VALUES(%s,?,?)" % (table, columns, placeholders),
                values + (now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict(conflict_message) from exc
        row = self.connection.execute("SELECT * FROM %s WHERE id=?" % table, (int(cursor.lastrowid),)).fetchone()
        return mapper(row)

    def insert_mission(
        self, reference: str, state: str, patient_id: int, aircraft_id: int, hospital_id: int, payload: Dict[str, Any], actor_id: str
    ) -> Dict[str, Any]:
        now = _now()
        try:
            cursor = self.connection.execute(
                "INSERT INTO missions(reference,state,version,patient_id,aircraft_id,hospital_id,payload,created_by,updated_by,created_at,updated_at)"
                " VALUES(?,?,1,?,?,?,?,?,?,?,?)",
                (reference, state, patient_id, aircraft_id, hospital_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("任务编号已存在") from exc
        row = self.connection.execute("SELECT * FROM missions WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        return Repository._mission_row(row)

    def update_mission(
        self,
        mission_id: int,
        expected_version: int,
        state: str,
        hospital_id: int,
        payload: Dict[str, Any],
        actor_id: str,
    ) -> Dict[str, Any]:
        row = self.connection.execute("SELECT version FROM missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            raise NotFound("转运任务不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        version = int(expected_version) + 1
        self.connection.execute(
            "UPDATE missions SET state=?,version=?,hospital_id=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, hospital_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, _now(), mission_id),
        )
        result = self.connection.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        return Repository._mission_row(result), version

    def update_patient(self, patient_id: int, state: Optional[str], mission_id: Any, payload_changes: Dict[str, Any]) -> Dict[str, Any]:
        row = self._must_exist("patients", patient_id, "伤员不存在")
        current = Repository._patient_row(row)
        payload = current["payload"]
        payload.update(payload_changes or {})
        new_state = state if state is not None else current["state"]
        if mission_id is not ...:
            new_mission = mission_id
        else:
            new_mission = current["mission_id"]
        self.connection.execute(
            "UPDATE patients SET state=?,mission_id=?,payload=?,updated_at=? WHERE id=?",
            (new_state, new_mission, json.dumps(payload, ensure_ascii=False, sort_keys=True), _now(), patient_id),
        )
        result = self.connection.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        return Repository._patient_row(result)

    # ---- 资源锁定/释放 ----
    def lock_aircraft(self, aircraft_id: int, mission_id: int, locked_slot: str) -> Dict[str, Any]:
        self.connection.execute(
            "UPDATE aircraft SET status='locked',locked_mission_id=?,locked_slot=?,updated_at=? WHERE id=?",
            (mission_id, locked_slot, _now(), aircraft_id),
        )
        return self.load_aircraft(aircraft_id)

    def unlock_aircraft(self, aircraft_id: int) -> Dict[str, Any]:
        self.connection.execute(
            "UPDATE aircraft SET status='available',locked_mission_id=NULL,locked_slot=NULL,updated_at=? WHERE id=?",
            (_now(), aircraft_id),
        )
        return self.load_aircraft(aircraft_id)

    def reserve_hospital(self, hospital_id: int) -> Dict[str, Any]:
        self._adjust_hospital(hospital_id, beds_reserved=1, slots_reserved=1)
        return self.load_hospital(hospital_id)

    def release_hospital_reservation(self, hospital_id: int) -> Dict[str, Any]:
        self._adjust_hospital(hospital_id, beds_reserved=-1, slots_reserved=-1)
        return self.load_hospital(hospital_id)

    def admit_hospital(self, hospital_id: int) -> Dict[str, Any]:
        """交接完成：预订床位转为占用，直升机机位释放。"""
        self._adjust_hospital(hospital_id, beds_reserved=-1, beds_used=1, slots_reserved=-1)
        return self.load_hospital(hospital_id)

    def _adjust_hospital(self, hospital_id: int, **deltas: int) -> None:
        row = self._must_exist("hospitals", hospital_id, "医院不存在")
        fields = ("beds_reserved", "slots_reserved", "beds_used")
        new_values = {}
        for field in fields:
            value = int(row[field]) + int(deltas.get(field, 0))
            if value < 0:
                value = 0
            new_values[field] = value
        self.connection.execute(
            "UPDATE hospitals SET beds_reserved=?,slots_reserved=?,beds_used=?,updated_at=? WHERE id=?",
            (new_values["beds_reserved"], new_values["slots_reserved"], new_values["beds_used"], _now(), hospital_id),
        )

    # ---- 审计 ----
    def add_audit(self, scope: str, target_id: int, action: str, actor_id: str, version: int, details: Dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO air_audit_events(scope,target_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
            (scope, target_id, action, actor_id, version, json.dumps(details or {}, ensure_ascii=False, sort_keys=True), _now()),
        )
