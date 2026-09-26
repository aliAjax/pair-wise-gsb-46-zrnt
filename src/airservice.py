"""空地转运用例编排：登记、派单核对、起飞锁定、改投降放、交接与时间线。"""
from typing import Any, Dict, List, Optional

from .airrules import AirRules
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository


class AirService:
    def __init__(self, repository: Repository, rules: AirRules = None) -> None:
        self.repository = repository
        self.rules = rules or AirRules()

    # ---- 身份 ----
    def _actor(self, actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    # ---- 登记 ----
    @staticmethod
    def _patient_view(row: Dict[str, Any]) -> Dict[str, Any]:
        return dict(row["payload"])

    @staticmethod
    def _aircraft_view(row: Dict[str, Any]) -> Dict[str, Any]:
        view = dict(row["payload"])
        view.update({"id": row["id"], "status": row["status"],
                     "locked_mission_id": row.get("locked_mission_id"),
                     "locked_slot": row.get("locked_slot")})
        return view

    @staticmethod
    def _hospital_view(row: Dict[str, Any]) -> Dict[str, Any]:
        view = dict(row["payload"])
        beds_available = int(row["beds_total"]) - int(row["beds_reserved"]) - int(row["beds_used"])
        slots_available = int(row["slots_total"]) - int(row["slots_reserved"])
        view.update({"id": row["id"], "beds_total": int(row["beds_total"]),
                     "beds_reserved": int(row["beds_reserved"]), "beds_used": int(row["beds_used"]),
                     "beds_available": beds_available, "slots_total": int(row["slots_total"]),
                     "slots_reserved": int(row["slots_reserved"]), "slots_available": slots_available})
        return view

    def register_patient(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create_patient(actor.role):
            raise PermissionDenied("角色无权登记伤员")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.validate_patient(payload or {})
        with self.repository.transaction() as tx:
            patient = tx.insert_patient(reference, prepared, actor.user_id)
            tx.add_audit("patient", patient["id"], "registered", actor.user_id, 1,
                         {"summary": "伤员登记", "payload": prepared})
        return patient

    def register_aircraft(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_register_resource(actor.role):
            raise PermissionDenied("角色无权登记直升机")
        prepared = self.rules.validate_aircraft(payload or {})
        with self.repository.transaction() as tx:
            aircraft = tx.insert_aircraft(prepared, actor.user_id)
            tx.add_audit("aircraft", aircraft["id"], "registered", actor.user_id, 1,
                         {"summary": "直升机登记：%s（%s）" % (prepared["aircraft_code"], prepared["base"]), "payload": prepared})
        return aircraft

    def register_hospital(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_register_resource(actor.role):
            raise PermissionDenied("角色无权登记医院")
        prepared = self.rules.validate_hospital(payload or {})
        with self.repository.transaction() as tx:
            hospital = tx.insert_hospital(prepared, actor.user_id)
            tx.add_audit("hospital", hospital["id"], "registered", actor.user_id, 1,
                         {"summary": "医院登记：%s（床位%s/机位%s）" % (prepared["name"], prepared["beds_total"], prepared["helipad_slots_total"]),
                          "payload": prepared})
        return hospital

    # ---- 查询 ----
    def get_patient(self, actor: Actor, patient_id: int) -> Dict[str, Any]:
        self._ensure_known_role(self._actor(actor))
        return self.repository.get_patient(patient_id)

    def get_aircraft(self, actor: Actor, aircraft_id: int) -> Dict[str, Any]:
        self._ensure_known_role(self._actor(actor))
        return self.repository.get_aircraft(aircraft_id)

    def get_hospital(self, actor: Actor, hospital_id: int) -> Dict[str, Any]:
        self._ensure_known_role(self._actor(actor))
        return self.repository.get_hospital(hospital_id)

    def get_mission(self, actor: Actor, mission_id: int) -> Dict[str, Any]:
        self._ensure_known_role(self._actor(actor))
        return self.repository.get_mission(mission_id)

    def list_patients(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        self._ensure_known_role(self._actor(actor))
        return self.repository.list_patients(state=state, limit=limit)

    def list_aircraft(self, actor: Actor, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        self._ensure_known_role(self._actor(actor))
        return self.repository.list_aircraft(status=status, limit=limit)

    def list_hospitals(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        self._ensure_known_role(self._actor(actor))
        return self.repository.list_hospitals(limit=limit)

    def list_missions(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        self._ensure_known_role(self._actor(actor))
        return self.repository.list_missions(state=state, limit=limit)

    # ---- 派单核对（只读） ----
    def evaluate(
        self,
        actor: Actor,
        patient_id: int,
        aircraft_id: int,
        hospital_id: int,
        raw_inputs: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._ensure_known_role(self._actor(actor))
        inputs = self.rules.validate_dispatch_input(raw_inputs or {})
        patient = self.repository.get_patient(patient_id)
        aircraft = self.repository.get_aircraft(aircraft_id)
        hospital = self.repository.get_hospital(hospital_id)
        report = self.rules.evaluate_dispatch(
            self._patient_view(patient), self._aircraft_view(aircraft), self._hospital_view(hospital), inputs)
        report["patient_id"] = patient_id
        report["aircraft_id"] = aircraft_id
        report["hospital_id"] = hospital_id
        return report

    # ---- 创建派单：核对不过则留在候派区并写明缺口 ----
    def create_mission(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create_mission(actor.role):
            raise PermissionDenied("角色无权创建转运任务")
        reference = text({"reference": reference}, "reference")
        p = dict(payload or {})
        patient_id = p.get("patient_id")
        aircraft_id = p.get("aircraft_id")
        hospital_id = p.get("hospital_id")
        if not isinstance(patient_id, int) or not isinstance(aircraft_id, int) or not isinstance(hospital_id, int):
            raise ValidationError("patient_id、aircraft_id、hospital_id必须是整数")
        inputs = self.rules.validate_dispatch_input(p)

        with self.repository.transaction() as tx:
            patient = tx.load_patient(patient_id)
            if patient["state"] in ("awaiting_dispatch", "assigned", "in_transit"):
                raise Conflict("伤员已有进行中的转运任务")
            aircraft = tx.load_aircraft(aircraft_id)
            hospital = tx.load_hospital(hospital_id)
            report = self.rules.evaluate_dispatch(
                self._patient_view(patient), self._aircraft_view(aircraft), self._hospital_view(hospital), inputs)
            state = self.rules.state_after_check(report["passed"])
            mission_payload = {
                "patient_id": patient_id,
                "aircraft_id": aircraft_id,
                "hospital_id": hospital_id,
                "inputs": inputs,
                "latest_report": report,
                "last_gaps": report["gaps"],
                "history": [{"stage": "create", "report": report}],
            }
            mission = tx.insert_mission(reference, state, patient_id, aircraft_id, hospital_id, mission_payload, actor.user_id)
            tx.update_patient(patient_id, "awaiting_dispatch" if state == "waiting_dispatch" else "assigned", mission["id"], {})
            tx.add_audit("mission", mission["id"], "created", actor.user_id, 1,
                         {"summary": "派单核对%s，任务%s" % ("通过" if report["passed"] else "未通过", "进入候派区" if not report["passed"] else "可起飞"),
                          "state": state, "passed": report["passed"], "gaps": report["gaps"]})
            tx.add_audit("patient", patient_id, "mission_created", actor.user_id, 1,
                         {"summary": "创建转运任务#%s，核对%s" % (mission["id"], "通过" if report["passed"] else "未通过"),
                          "mission_id": mission["id"], "gaps": report["gaps"]})
        return self.repository.get_mission(mission["id"])

    # ---- 任务动作 ----
    def act(self, actor: Actor, mission_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if not isinstance(expected_version, int):
            raise ValidationError("expected_version必须是整数")
        data = data or {}
        with self.repository.transaction() as tx:
            mission = tx.load_mission(mission_id)
            self.rules.require_transition(mission["state"], action)
            handler = {
                "recheck": self._do_recheck,
                "takeoff": self._do_takeoff,
                "divert": self._do_divert,
                "handover": self._do_handover,
                "cancel": self._do_cancel,
            }.get(action)
            if handler is None:
                raise ValidationError("未知操作：%s" % action)
            mission = handler(tx, mission, expected_version, actor.user_id, data)
        return self.repository.get_mission(mission_id)

    def _save_report(self, tx, mission: Dict[str, Any], expected_version: int, actor_id: str, action: str,
                     state: str, report: Dict[str, Any], summary: str, extra_payload: Dict[str, Any] = None) -> Dict[str, Any]:
        payload = dict(mission["payload"])
        payload["latest_report"] = report
        payload["last_gaps"] = report["gaps"]
        payload.setdefault("history", []).append({"stage": action, "report": report})
        if extra_payload:
            payload.update(extra_payload)
        updated, version = tx.update_mission(mission["id"], expected_version, state, mission["hospital_id"], payload, actor_id)
        tx.add_audit("mission", mission["id"], action, actor_id, version,
                     {"summary": summary, "from": mission["state"], "to": state, "passed": report["passed"], "gaps": report["gaps"]})
        return updated

    def _do_recheck(self, tx, mission, expected_version, actor_id, data) -> Dict[str, Any]:
        inputs = self.rules.validate_dispatch_input(data)
        patient = tx.load_patient(mission["patient_id"])
        aircraft = tx.load_aircraft(mission["aircraft_id"])
        hospital = tx.load_hospital(mission["hospital_id"])
        report = self.rules.evaluate_dispatch(
            self._patient_view(patient), self._aircraft_view(aircraft), self._hospital_view(hospital), inputs)
        state = self.rules.state_after_check(report["passed"])
        patient_state = "assigned" if state == "assigned" else "awaiting_dispatch"
        tx.update_patient(mission["patient_id"], patient_state, mission["id"], {})
        summary = "重新核对通过，可起飞" if report["passed"] else "重新核对仍有缺口，留在候派区"
        return self._save_report(tx, mission, expected_version, actor_id, "recheck", state, report,
                                 summary, extra_payload={"inputs": inputs})

    def _do_takeoff(self, tx, mission, expected_version, actor_id, data) -> Dict[str, Any]:
        # 起飞前用最新余量再核一遍；通过才锁定直升机和机位
        inputs = mission["payload"]["inputs"]
        patient = tx.load_patient(mission["patient_id"])
        aircraft = tx.load_aircraft(mission["aircraft_id"])
        hospital = tx.load_hospital(mission["hospital_id"])
        report = self.rules.evaluate_dispatch(
            self._patient_view(patient), self._aircraft_view(aircraft), self._hospital_view(hospital), inputs)
        if not report["passed"]:
            raise ValidationError("起飞核对未通过，请先重新核对并补齐缺口：%s" % "；".join(g["message"] for g in report["gaps"]))
        # 锁定：直升机占用 + 医院床位/机位预留
        locked_aircraft = tx.lock_aircraft(mission["aircraft_id"], mission["id"], "%s#slot" % hospital["name"])
        tx.reserve_hospital(mission["hospital_id"])
        payload = dict(mission["payload"])
        payload["latest_report"] = report
        payload.setdefault("history", []).append({"stage": "takeoff", "report": report})
        payload["takeoff_lock"] = {
            "aircraft_code": locked_aircraft["aircraft_code"],
            "hospital_id": mission["hospital_id"],
            "hospital_name": hospital["name"],
            "slot": locked_aircraft["locked_slot"],
        }
        updated, version = tx.update_mission(mission["id"], expected_version, "airborne", mission["hospital_id"], payload, actor_id)
        tx.update_patient(mission["patient_id"], "in_transit", mission["id"], {})
        tx.add_audit("mission", mission["id"], "takeoff", actor_id, version,
                     {"summary": "已起飞，锁定直升机%s与%s床位/机位各1" % (locked_aircraft["aircraft_code"], hospital["name"]),
                      "from": "assigned", "to": "airborne", "lock": payload["takeoff_lock"]})
        tx.add_audit("patient", mission["patient_id"], "takeoff", actor_id, version,
                     {"summary": "已乘%s起飞，目的地：%s" % (locked_aircraft["aircraft_code"], hospital["name"]),
                      "mission_id": mission["id"]})
        return updated

    def _do_divert(self, tx, mission, expected_version, actor_id, data) -> Dict[str, Any]:
        divert = self.rules.validate_divert_input(data)
        inputs = mission["payload"]["inputs"]
        patient = tx.load_patient(mission["patient_id"])
        aircraft = tx.load_aircraft(mission["aircraft_id"])
        target = tx.load_hospital(divert["hospital_id"])
        report = self.rules.evaluate_diversion(
            self._patient_view(patient), self._aircraft_view(aircraft), inputs, self._hospital_view(target), divert)
        if not report["passed"]:
            raise ValidationError("改降核对未通过：%s" % "；".join(g["message"] for g in report["gaps"]))
        # 先释放原医院床位/机位，再锁定新医院
        old_hospital = tx.load_hospital(mission["hospital_id"])
        tx.release_hospital_reservation(old_hospital["id"])
        tx.reserve_hospital(target["id"])
        tx.lock_aircraft(mission["aircraft_id"], mission["id"], "%s#slot" % target["name"])
        new_inputs = dict(inputs)
        new_inputs["scene_to_hospital_km"] = divert["scene_to_hospital_km"]
        new_inputs["hospital_to_base_km"] = divert["hospital_to_base_km"]
        payload = dict(mission["payload"])
        payload["inputs"] = new_inputs
        payload["latest_report"] = report
        payload.setdefault("history", []).append({"stage": "divert", "report": report, "from_hospital_id": old_hospital["id"], "to_hospital_id": target["id"]})
        payload["takeoff_lock"] = {
            "aircraft_code": aircraft["aircraft_code"],
            "hospital_id": target["id"],
            "hospital_name": target["name"],
            "slot": "%s#slot" % target["name"],
        }
        updated, version = tx.update_mission(mission["id"], expected_version, "airborne", target["id"], payload, actor_id)
        tx.update_patient(mission["patient_id"], "in_transit", mission["id"],
                          {"diverted_to_hospital_id": target["id"], "diverted_to_hospital_name": target["name"]})
        tx.add_audit("mission", mission["id"], "divert", actor_id, version,
                     {"summary": "改降%s，已先释放%s预留" % (target["name"], old_hospital["name"]),
                      "from_hospital": old_hospital["name"], "to_hospital": target["name"],
                      "reason": divert.get("reason", ""), "gaps": report["gaps"]})
        tx.add_audit("patient", mission["patient_id"], "divert", actor_id, version,
                     {"summary": "目的地由%s改为%s" % (old_hospital["name"], target["name"]),
                      "mission_id": mission["id"], "reason": divert.get("reason", "")})
        return updated

    def _do_handover(self, tx, mission, expected_version, actor_id, data) -> Dict[str, Any]:
        if not isinstance(data.get("handover_accepted"), bool) or not data.get("handover_accepted"):
            raise ValidationError("医院尚未接收")
        hospital = tx.load_hospital(mission["hospital_id"])
        aircraft = tx.load_aircraft(mission["aircraft_id"])
        note = str(data.get("handover_note", "")).strip()
        # 预留转占用，机位释放，直升机解锁
        tx.admit_hospital(hospital["id"])
        tx.unlock_aircraft(mission["aircraft_id"])
        payload = dict(mission["payload"])
        payload["handover_note"] = note
        updated, version = tx.update_mission(mission["id"], expected_version, "closed", mission["hospital_id"], payload, actor_id)
        tx.update_patient(mission["patient_id"], "delivered", mission["id"],
                          {"delivered_hospital_id": hospital["id"], "delivered_hospital_name": hospital["name"], "handover_note": note})
        tx.add_audit("mission", mission["id"], "handover", actor_id, version,
                     {"summary": "交接完成：%s，直升机%s已释放" % (hospital["name"], aircraft["aircraft_code"]),
                      "from": "airborne", "to": "closed"})
        tx.add_audit("patient", mission["patient_id"], "handover", actor_id, version,
                     {"summary": "已在%s完成交接" % hospital["name"], "mission_id": mission["id"], "note": note})
        return updated

    def _do_cancel(self, tx, mission, expected_version, actor_id, data) -> Dict[str, Any]:
        reason = str(data.get("cancel_reason", "")).strip()
        if not reason:
            raise ValidationError("cancel_reason不能为空")
        # 仅候派/已核对未起飞可取消，此时没有任何资源被锁定
        payload = dict(mission["payload"])
        payload["cancel_reason"] = reason
        updated, version = tx.update_mission(mission["id"], expected_version, "cancelled", mission["hospital_id"], payload, actor_id)
        tx.update_patient(mission["patient_id"], "awaiting_dispatch", None, {})
        tx.add_audit("mission", mission["id"], "cancel", actor_id, version,
                     {"summary": "任务取消：%s" % reason, "from": mission["state"], "to": "cancelled", "reason": reason})
        tx.add_audit("patient", mission["patient_id"], "mission_cancelled", actor_id, version,
                     {"summary": "转运任务#%s取消：%s" % (mission["id"], reason), "mission_id": mission["id"]})
        return updated

    # ---- 时间线与统计 ----
    def patient_timeline(self, actor: Actor, patient_id: int) -> List[Dict[str, Any]]:
        """沿伤员看到每次评估和去向：登记、派单核对、起飞、改降、交接。"""
        self._ensure_known_role(self._actor(actor))
        self.repository.get_patient(patient_id)
        return self.repository.air_timeline("patient", patient_id)

    def mission_timeline(self, actor: Actor, mission_id: int) -> List[Dict[str, Any]]:
        self._ensure_known_role(self._actor(actor))
        self.repository.get_mission(mission_id)
        return self.repository.air_timeline("mission", mission_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        self._ensure_known_role(self._actor(actor))
        return self.repository.air_stats()
