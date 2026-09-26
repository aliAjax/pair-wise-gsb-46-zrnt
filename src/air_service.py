"""空地转运用例编排：登记、派单核对、起飞锁定、改释放床、交接与时间线。"""
from typing import Any, Dict, List, Optional

from .air_rules import (
    CRAFT_AIRBORNE,
    CRAFT_RESERVED,
    PATIENT_AIRBORNE,
    PATIENT_ASSIGNED,
    PATIENT_WAITING,
    AirRules,
)
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository


class AirService:
    def __init__(self, repository: Repository, rules: AirRules = None) -> None:
        self.repository = repository
        self.rules = rules or AirRules()

    # ---- 身份与权限 ----

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _require_create_role(self, actor: Actor) -> None:
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权登记资源")

    def _require_action_role(self, actor: Actor, action: str) -> None:
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")

    # ---- 视图：补充派生余量 ----

    def _helicopter_view(self, craft: Dict[str, Any], patients: List[Dict[str, Any]]) -> Dict[str, Any]:
        view = dict(craft)
        view["occupied_seats"] = self.rules.occupied_slots(patients, craft["id"])
        view["available_seats"] = int(craft["payload"]["seats"]) - view["occupied_seats"]
        return view

    def _hospital_view(self, hospital: Dict[str, Any], patients: List[Dict[str, Any]]) -> Dict[str, Any]:
        view = dict(hospital)
        occupied = self.rules.occupied_beds(patients, hospital["id"])
        view["occupied_slots"] = occupied
        view["remaining_slots"] = int(hospital["payload"]["total_slots"]) - occupied
        return view

    # ---- 登记 ----

    def register_patient(self, actor: Actor, code: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_create_role(actor)
        code = text({"code": code}, "code")
        prepared = self.rules.validate_patient(payload or {})
        return self.repository.entity_create("patient", code, self.rules.INITIAL_PATIENT_STATE, prepared, actor.user_id)

    def register_helicopter(self, actor: Actor, code: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_create_role(actor)
        code = text({"code": code}, "code")
        prepared = self.rules.validate_helicopter(payload or {})
        return self.repository.entity_create("helicopter", code, self.rules.INITIAL_CRAFT_STATE, prepared, actor.user_id)

    def register_hospital(self, actor: Actor, code: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_create_role(actor)
        code = text({"code": code}, "code")
        prepared = self.rules.validate_hospital(payload or {})
        return self.repository.entity_create("hospital", code, self.rules.INITIAL_HOSPITAL_STATE, prepared, actor.user_id)

    # ---- 查询 ----

    def list_patients(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.entity_list("patient", state=state, limit=limit)

    def list_helicopters(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        patients = self.repository.entity_list("patient", limit=500)
        return [self._helicopter_view(c, patients) for c in self.repository.entity_list("helicopter", state=state, limit=limit)]

    def list_hospitals(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        patients = self.repository.entity_list("patient", limit=500)
        return [self._hospital_view(h, patients) for h in self.repository.entity_list("hospital", limit=limit)]

    def get_patient(self, actor: Actor, patient_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.entity_get("patient", patient_id)

    def get_helicopter(self, actor: Actor, helicopter_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._helicopter_view(
            self.repository.entity_get("helicopter", helicopter_id),
            self.repository.entity_list("patient", limit=500),
        )

    def get_hospital(self, actor: Actor, hospital_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._hospital_view(
            self.repository.entity_get("hospital", hospital_id),
            self.repository.entity_list("patient", limit=500),
        )

    # ---- 派单核对 ----

    def evaluate_dispatch(self, actor: Actor, patient_id: int, helicopter_id: int, hospital_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "evaluate_dispatch")
        patient = self.repository.entity_get("patient", patient_id)
        craft = self.repository.entity_get("helicopter", helicopter_id)
        hospital = self.repository.entity_get("hospital", hospital_id)
        normalized = self.rules.normalize_dispatch_input(data or {})
        all_patients = self.repository.entity_list("patient", limit=500)
        gaps, metrics = self.rules.assess_dispatch(patient, craft, hospital, normalized, all_patients)

        if gaps:
            # 留在候派区，写明缺口；记录本次核对尝试并递增版本
            payload = dict(patient["payload"])
            payload["last_gap"] = {
                "helicopter_id": craft["id"],
                "hospital_id": hospital["id"],
                "gaps": gaps,
                "metrics": metrics,
            }
            self.repository.atomic_batch(
                changes=[
                    {
                        "entity_type": "patient",
                        "entity_id": patient["id"],
                        "expected_version": patient["version"],
                        "state": patient["state"],
                        "payload": payload,
                        "actor_id": actor.user_id,
                    }
                ],
                events=[
                    {
                        "entity_type": "patient",
                        "entity_id": patient["id"],
                        "action": "dispatch_checked",
                        "actor_id": actor.user_id,
                        "details": {"passed": False, "gaps": gaps, "metrics": metrics,
                                    "helicopter_id": craft["id"], "hospital_id": hospital["id"]},
                    }
                ],
            )
            return {"passed": False, "gaps": gaps, "metrics": metrics, "patient_state": PATIENT_WAITING}

        # 通过：派单，锁定机位与床位（预留）
        new_payload = self.rules.apply_dispatch_success(patient, craft, hospital, normalized, metrics)
        # 同机已有空中伤员会在 assess_dispatch 阶段拦截；成功派单后机位至少被本伤员占用
        new_craft_state = CRAFT_RESERVED
        batch = [
            {"entity_type": "patient", "entity_id": patient["id"], "expected_version": patient["version"],
             "state": PATIENT_ASSIGNED, "payload": new_payload, "actor_id": actor.user_id},
            {"entity_type": "helicopter", "entity_id": craft["id"], "expected_version": craft["version"],
             "state": new_craft_state, "payload": craft["payload"], "actor_id": actor.user_id},
            {"entity_type": "hospital", "entity_id": hospital["id"], "expected_version": hospital["version"],
             "state": hospital["state"], "payload": hospital["payload"], "actor_id": actor.user_id},
        ]
        events = [
            {"entity_type": "patient", "entity_id": patient["id"], "action": "dispatch_passed", "actor_id": actor.user_id,
             "details": {"gaps": [], "metrics": metrics, "helicopter_id": craft["id"], "hospital_id": hospital["id"],
                         "from": patient["state"], "to": PATIENT_ASSIGNED}},
            {"entity_type": "helicopter", "entity_id": craft["id"], "action": "seat_reserved", "actor_id": actor.user_id,
             "details": {"patient_id": patient["id"], "state": new_craft_state}},
            {"entity_type": "hospital", "entity_id": hospital["id"], "action": "slot_reserved", "actor_id": actor.user_id,
             "details": {"patient_id": patient["id"]}},
        ]
        rows = self.repository.atomic_batch(batch, events)
        updated_patient = rows["patient"][0]
        patients = self.repository.entity_list("patient", limit=500)
        return {
            "passed": True,
            "gaps": [],
            "metrics": metrics,
            "patient": updated_patient,
            "helicopter": self._helicopter_view(rows["helicopter"][0], patients),
            "hospital": self._hospital_view(rows["hospital"][0], patients),
        }

    # ---- 起飞：整机锁定 ----

    def takeoff(self, actor: Actor, patient_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "takeoff")
        self.rules.validate_takeoff(data or {})
        patient = self.repository.entity_get("patient", patient_id)
        new_state = self.rules.require_transition(patient, "takeoff")
        craft_id = int(patient["payload"].get("helicopter_id", 0))
        if not craft_id:
            raise Conflict("伤员尚未派单")
        craft = self.repository.entity_get("helicopter", craft_id)

        all_patients = self.repository.entity_list("patient", limit=500)
        batch = [
            {"entity_type": "patient", "entity_id": patient["id"], "expected_version": expected_version,
             "state": new_state, "payload": patient["payload"], "actor_id": actor.user_id},
            {"entity_type": "helicopter", "entity_id": craft_id, "expected_version": craft["version"],
             "state": CRAFT_AIRBORNE, "payload": craft["payload"], "actor_id": actor.user_id},
        ]
        events = [
            {"entity_type": "patient", "entity_id": patient["id"], "action": "takeoff", "actor_id": actor.user_id,
             "details": {"from": patient["state"], "to": new_state}},
            {"entity_type": "helicopter", "entity_id": craft_id, "action": "airborne_locked", "actor_id": actor.user_id,
             "details": {"patient_id": patient["id"], "state": CRAFT_AIRBORNE}},
        ]
        # 同机其他已派单伤员随机起飞
        linked = [
            p for p in all_patients
            if p["state"] == PATIENT_ASSIGNED and p["payload"].get("helicopter_id") == craft_id and p["id"] != patient["id"]
        ]
        for other in linked:
            batch.append({"entity_type": "patient", "entity_id": other["id"], "expected_version": other["version"],
                          "state": PATIENT_AIRBORNE, "payload": other["payload"], "actor_id": actor.user_id})
            events.append({"entity_type": "patient", "entity_id": other["id"], "action": "takeoff", "actor_id": actor.user_id,
                           "details": {"from": PATIENT_ASSIGNED, "to": PATIENT_AIRBORNE, "reason": "same_aircraft"}})
        rows = self.repository.atomic_batch(batch, events)
        return rows["patient"][0]

    # ---- 改降：先释放原床位 ----

    def divert(self, actor: Actor, patient_id: int, expected_version: int, target_hospital_id: int, note: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "divert")
        patient = self.repository.entity_get("patient", patient_id)
        self.rules.require_transition(patient, "divert")
        target = self.repository.entity_get("hospital", target_hospital_id)
        original_id = int(patient["payload"].get("hospital_id", 0))
        if not original_id:
            raise Conflict("伤员尚未派单")
        original = self.repository.entity_get("hospital", original_id)
        new_payload = self.rules.divert_payload(patient, target)

        all_patients = self.repository.entity_list("patient", limit=500)
        # 原床位必须先释放，目标床位要有余量
        target_remaining = int(target["payload"]["total_slots"]) - self.rules.occupied_beds(all_patients, target["id"])
        if target_remaining <= 0:
            raise Conflict("改降目标医院无余量机位")
        batch = [
            {"entity_type": "patient", "entity_id": patient["id"], "expected_version": expected_version,
             "state": patient["state"], "payload": new_payload, "actor_id": actor.user_id},
            {"entity_type": "hospital", "entity_id": original_id, "expected_version": original["version"],
             "state": original["state"], "payload": original["payload"], "actor_id": actor.user_id},
            {"entity_type": "hospital", "entity_id": target["id"], "expected_version": target["version"],
             "state": target["state"], "payload": target["payload"], "actor_id": actor.user_id},
        ]
        events = [
            {"entity_type": "patient", "entity_id": patient["id"], "action": "diverted", "actor_id": actor.user_id,
             "details": {"from_hospital_id": original_id, "to_hospital_id": target["id"], "note": note}},
            {"entity_type": "hospital", "entity_id": original_id, "action": "slot_released", "actor_id": actor.user_id,
             "details": {"patient_id": patient["id"], "reason": "divert"}},
            {"entity_type": "hospital", "entity_id": target["id"], "action": "slot_reserved", "actor_id": actor.user_id,
             "details": {"patient_id": patient["id"], "reason": "divert"}},
        ]
        rows = self.repository.atomic_batch(batch, events)
        patients = self.repository.entity_list("patient", limit=500)
        return {
            "patient": rows["patient"][0],
            "from_hospital": self._hospital_view(rows["hospital"][0], patients),
            "to_hospital": self._hospital_view(rows["hospital"][1], patients),
        }

    # ---- 交接：床位转实际占用，直升机释放 ----

    def handover(self, actor: Actor, patient_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "handover")
        self.rules.validate_handover(data or {})
        patient = self.repository.entity_get("patient", patient_id)
        new_state = self.rules.require_transition(patient, "handover")
        payload = dict(patient["payload"])
        payload["handover"] = {"accepted": True, "note": (data or {}).get("note", "")}

        craft_id = int(payload.get("helicopter_id", 0))
        craft = self.repository.entity_get("helicopter", craft_id) if craft_id else None
        all_patients = self.repository.entity_list("patient", limit=500)
        batch = [
            {"entity_type": "patient", "entity_id": patient["id"], "expected_version": expected_version,
             "state": new_state, "payload": payload, "actor_id": actor.user_id},
        ]
        events = [
            {"entity_type": "patient", "entity_id": patient["id"], "action": "handover", "actor_id": actor.user_id,
             "details": {"from": patient["state"], "to": new_state, "hospital_id": payload.get("hospital_id")}},
        ]
        new_craft_state = None
        if craft is not None:
            new_craft_state = self.rules.craft_state_after(
                craft, [p for p in all_patients if p["id"] != patient["id"]]
            )
            batch.append({"entity_type": "helicopter", "entity_id": craft_id, "expected_version": craft["version"],
                          "state": new_craft_state, "payload": craft["payload"], "actor_id": actor.user_id})
            events.append({"entity_type": "helicopter", "entity_id": craft_id,
                           "action": "returned_available" if new_craft_state == "available" else "craft_state_updated",
                           "actor_id": actor.user_id, "details": {"patient_id": patient["id"], "state": new_craft_state}})
        rows = self.repository.atomic_batch(batch, events)
        result = {"patient": rows["patient"][0]}
        if new_craft_state is not None:
            patients = self.repository.entity_list("patient", limit=500)
            result["helicopter"] = self._helicopter_view(rows["helicopter"][0], patients)
        return result

    # ---- 取消：释放机位与床位 ----

    def cancel(self, actor: Actor, patient_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "cancel")
        reason = self.rules.cancel_reason(data or {})
        patient = self.repository.entity_get("patient", patient_id)
        new_state = self.rules.require_transition(patient, "cancel")
        payload = dict(patient["payload"])
        payload["cancel_reason"] = reason

        craft_id = int(payload.get("helicopter_id") or 0)
        hospital_id = int(payload.get("hospital_id") or 0)
        was_assigned = patient["state"] in {PATIENT_ASSIGNED, PATIENT_AIRBORNE}
        craft = self.repository.entity_get("helicopter", craft_id) if was_assigned and craft_id else None
        hospital = self.repository.entity_get("hospital", hospital_id) if was_assigned and hospital_id else None

        all_patients = self.repository.entity_list("patient", limit=500)
        batch = [
            {"entity_type": "patient", "entity_id": patient["id"], "expected_version": expected_version,
             "state": new_state, "payload": payload, "actor_id": actor.user_id},
        ]
        events = [
            {"entity_type": "patient", "entity_id": patient["id"], "action": "cancelled", "actor_id": actor.user_id,
             "details": {"from": patient["state"], "to": new_state, "reason": reason}},
        ]
        if craft is not None:
            new_craft_state = self.rules.craft_state_after(
                craft, [p for p in all_patients if p["id"] != patient["id"]]
            )
            batch.append({"entity_type": "helicopter", "entity_id": craft_id, "expected_version": craft["version"],
                          "state": new_craft_state, "payload": craft["payload"], "actor_id": actor.user_id})
            events.append({"entity_type": "helicopter", "entity_id": craft_id, "action": "seat_released",
                           "actor_id": actor.user_id, "details": {"patient_id": patient["id"], "state": new_craft_state}})
        if hospital is not None:
            batch.append({"entity_type": "hospital", "entity_id": hospital_id, "expected_version": hospital["version"],
                          "state": hospital["state"], "payload": hospital["payload"], "actor_id": actor.user_id})
            events.append({"entity_type": "hospital", "entity_id": hospital_id, "action": "slot_released",
                           "actor_id": actor.user_id, "details": {"patient_id": patient["id"], "reason": "cancel"}})
        rows = self.repository.atomic_batch(batch, events)
        return rows["patient"][0]

    # ---- 时间线与总览 ----

    def patient_timeline(self, actor: Actor, patient_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.entity_timeline("patient", patient_id)

    def overview(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.air_stats()
