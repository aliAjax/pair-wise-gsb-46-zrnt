"""空地转运领域规则：资源登记校验、派单核对与状态转换。

电话确认项（天气、医院收治条件）由调度员在派单数据中显式确认，
系统同时根据登记数据核航程、载重与余量；任何一项不满足都生成
结构化缺口，任务留在候派区。
"""
from typing import Any, Dict, List, Optional, Tuple

from .domain import Actor, Conflict, boolean, choice, integer, number, optional_text, text


# 固定任务重量：机组与急救设备（kg）
CREW_AND_GEAR_KG = 200.0
# 氧源钢具重量：需氧量大于0时装载（kg）
OXYGEN_CYLINDER_KG = 12.0
# 航程备份（km）
RANGE_RESERVE_KM = 20.0

SEVERITY_CAPABILITY = {"critical": "ALS", "urgent": "ALS", "stable": "BLS"}

MISSION_CREATE_ROLES = {"dispatcher"}
PATIENT_CREATE_ROLES = {"dispatcher", "paramedic"}
RESOURCE_ROLES = {"dispatcher"}
ACTION_ROLES = {
    "recheck": {"dispatcher"},
    "takeoff": {"dispatcher", "pilot"},
    "divert": {"dispatcher", "pilot"},
    "handover": {"paramedic", "hospital_coordinator"},
    "cancel": {"dispatcher"},
}
TRANSITIONS = {
    "recheck": {"waiting_dispatch", "assigned"},
    "takeoff": {"assigned"},
    "divert": {"airborne"},
    "handover": {"airborne"},
    "cancel": {"waiting_dispatch", "assigned"},
}
DISPATCH_STATES = ("waiting_dispatch", "assigned", "airborne", "closed", "cancelled")


def _gap(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


class AirRules:
    """纯规则计算，不访问数据库，便于单测。"""

    # ---- 身份与权限 ----
    def known_role(self, role: str) -> bool:
        all_roles = set(MISSION_CREATE_ROLES) | set(PATIENT_CREATE_ROLES) | set(RESOURCE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create_patient(self, role: str) -> bool:
        return role == "admin" or role in PATIENT_CREATE_ROLES

    def role_can_register_resource(self, role: str) -> bool:
        return role == "admin" or role in RESOURCE_ROLES

    def role_can_create_mission(self, role: str) -> bool:
        return role == "admin" or role in MISSION_CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    # ---- 登记校验 ----
    def validate_patient(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        prepared: Dict[str, Any] = {}
        prepared["injury_severity"] = choice(p, "injury_severity", ["critical", "urgent", "stable"])
        prepared["injury_note"] = optional_text(p, "injury_note")
        prepared["weight_kg"] = round(number(p, "weight_kg", 1, 400), 1)
        prepared["oxygen_lpm"] = round(number(p, "oxygen_lpm", 0, 30), 1)
        prepared["pickup_location"] = text(p, "pickup_location")
        prepared["needs_oxygen"] = prepared["oxygen_lpm"] > 0
        return prepared

    def validate_aircraft(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        prepared: Dict[str, Any] = {}
        prepared["aircraft_code"] = text(p, "aircraft_code")
        prepared["base"] = text(p, "base")
        prepared["slot"] = text(p, "slot")
        prepared["endurance_km"] = round(number(p, "endurance_km", 1, 5000), 1)
        prepared["payload_capacity_kg"] = round(number(p, "payload_capacity_kg", 1, 2000), 1)
        prepared["night_qualified"] = boolean(p, "night_qualified", False)
        prepared["oxygen_kit"] = boolean(p, "oxygen_kit", False)
        return prepared

    def validate_hospital(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        prepared: Dict[str, Any] = {}
        prepared["name"] = text(p, "name")
        prepared["beds_total"] = integer(p, "beds_total", 0, 100000)
        prepared["helipad_slots_total"] = integer(p, "helipad_slots_total", 0, 100)
        prepared["night_receiving"] = boolean(p, "night_receiving", False)
        prepared["oxygen_supply"] = boolean(p, "oxygen_supply", False)
        prepared["capability"] = choice(p, "capability", ["BLS", "ALS"])
        return prepared

    def validate_dispatch_input(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """派单时电话确认的天气、医院确认与三段里程。"""
        p = dict(payload or {})
        return {
            "weather_confirmed": boolean(p, "weather_confirmed", False),
            "weather_flyable": boolean(p, "weather_flyable", False),
            "weather_summary": optional_text(p, "weather_summary"),
            "hospital_confirmed": boolean(p, "hospital_confirmed", False),
            "night_flight": boolean(p, "night_flight", False),
            "base_to_scene_km": round(number(p, "base_to_scene_km", 0, 3000), 1),
            "scene_to_hospital_km": round(number(p, "scene_to_hospital_km", 0, 3000), 1),
            "hospital_to_base_km": round(number(p, "hospital_to_base_km", 0, 3000), 1),
        }

    def validate_divert_input(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        return {
            "hospital_id": integer(p, "hospital_id", 1),
            "hospital_confirmed": boolean(p, "hospital_confirmed", False),
            "scene_to_hospital_km": round(number(p, "scene_to_hospital_km", 0, 3000), 1),
            "hospital_to_base_km": round(number(p, "hospital_to_base_km", 0, 3000), 1),
            "reason": optional_text(p, "reason"),
        }

    # ---- 状态转换 ----
    def require_transition(self, state: str, action: str) -> None:
        if state not in TRANSITIONS.get(action, set()):
            raise Conflict("当前状态不允许执行%s" % action)

    @staticmethod
    def state_after_check(passed: bool) -> str:
        return "assigned" if passed else "waiting_dispatch"

    # ---- 核对计算 ----
    @staticmethod
    def _hospital_gaps(patient: Dict[str, Any], inputs: Dict[str, Any], hospital: Dict[str, Any]) -> List[Dict[str, str]]:
        gaps: List[Dict[str, str]] = []
        required_capability = SEVERITY_CAPABILITY[patient["injury_severity"]]
        if required_capability == "ALS" and hospital["capability"] != "ALS":
            gaps.append(_gap("hospital_capability", "医院%s为%s级别，不具备%s收治能力" % (hospital["name"], hospital["capability"], required_capability)))
        if patient.get("needs_oxygen") and not hospital["oxygen_supply"]:
            gaps.append(_gap("hospital_oxygen", "医院%s无氧源，无法满足伤员%sL/min需氧" % (hospital["name"], patient["oxygen_lpm"])))
        if inputs.get("night_flight") and not hospital["night_receiving"]:
            gaps.append(_gap("hospital_night", "医院%s不具备夜间接收条件" % hospital["name"]))
        if int(hospital["beds_available"]) <= 0:
            gaps.append(_gap("hospital_bed", "医院%s床位余量为0" % hospital["name"]))
        if int(hospital["slots_available"]) <= 0:
            gaps.append(_gap("hospital_slot", "医院%s直升机机位余量为0" % hospital["name"]))
        return gaps

    def evaluate_dispatch(
        self,
        patient: Dict[str, Any],
        aircraft: Dict[str, Any],
        hospital: Dict[str, Any],
        inputs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """派单核对：天气、夜间资质、往返里程、载重、医院余量与收治条件。"""
        gaps: List[Dict[str, str]] = []

        # 天气（电话确认）
        if not inputs["weather_confirmed"]:
            gaps.append(_gap("weather_unconfirmed", "天气未经电话确认，不能派单"))
        elif not inputs["weather_flyable"]:
            gaps.append(_gap("weather_unfit", "电话通报天气不适航：%s" % (inputs["weather_summary"] or "无说明")))

        # 直升机占用：起飞即锁定，已锁定的不能再派
        if aircraft["status"] != "available":
            gaps.append(_gap("aircraft_locked", "直升机%s已被任务%s锁定" % (aircraft["aircraft_code"], aircraft.get("locked_mission_id") or "")))

        # 夜间资质
        if inputs["night_flight"] and not aircraft["night_qualified"]:
            gaps.append(_gap("aircraft_night", "直升机%s无夜间飞行资质" % aircraft["aircraft_code"]))

        # 氧源
        if patient.get("needs_oxygen") and not aircraft["oxygen_kit"]:
            gaps.append(_gap("aircraft_oxygen", "直升机%s未配氧源，无法支持%sL/min需氧量" % (aircraft["aircraft_code"], patient["oxygen_lpm"])))

        # 往返里程：基地→接救点→医院→基地，含备份
        roundtrip = inputs["base_to_scene_km"] + inputs["scene_to_hospital_km"] + inputs["hospital_to_base_km"]
        required_km = round(roundtrip + RANGE_RESERVE_KM, 1)
        range_shortfall = round(required_km - aircraft["endurance_km"], 1)
        if range_shortfall > 0:
            gaps.append(_gap("range", "往返%skm+备份%skm=%skm，超过%s续航%skm，缺口%skm" % (
                round(roundtrip, 1), RANGE_RESERVE_KM, required_km, aircraft["aircraft_code"], aircraft["endurance_km"], range_shortfall)))

        # 载重：伤员体重 + 氧源 + 机组设备
        oxygen_kg = OXYGEN_CYLINDER_KG if patient.get("needs_oxygen") else 0.0
        required_load = round(patient["weight_kg"] + oxygen_kg + CREW_AND_GEAR_KG, 1)
        load_shortfall = round(required_load - aircraft["payload_capacity_kg"], 1)
        if load_shortfall > 0:
            gaps.append(_gap("payload", "总载重%skg（伤员%s+氧源%s+机组设备%s）超过%s可用载重%skg，缺口%skg" % (
                required_load, patient["weight_kg"], oxygen_kg, CREW_AND_GEAR_KG, aircraft["aircraft_code"], aircraft["payload_capacity_kg"], load_shortfall)))

        # 医院电话确认
        if not inputs["hospital_confirmed"]:
            gaps.append(_gap("hospital_unconfirmed", "医院%s收治条件未经电话确认" % hospital["name"]))

        gaps.extend(self._hospital_gaps(patient, inputs, hospital))

        return {
            "passed": not gaps,
            "gaps": gaps,
            "weather": {
                "confirmed": inputs["weather_confirmed"],
                "flyable": inputs["weather_flyable"],
                "summary": inputs["weather_summary"],
            },
            "night_flight": inputs["night_flight"],
            "range": {
                "base_to_scene_km": inputs["base_to_scene_km"],
                "scene_to_hospital_km": inputs["scene_to_hospital_km"],
                "hospital_to_base_km": inputs["hospital_to_base_km"],
                "roundtrip_km": round(roundtrip, 1),
                "reserve_km": RANGE_RESERVE_KM,
                "required_km": required_km,
                "endurance_km": aircraft["endurance_km"],
                "shortfall_km": max(range_shortfall, 0.0),
            },
            "load": {
                "patient_weight_kg": patient["weight_kg"],
                "oxygen_kit_kg": oxygen_kg,
                "crew_gear_kg": CREW_AND_GEAR_KG,
                "required_kg": required_load,
                "capacity_kg": aircraft["payload_capacity_kg"],
                "shortfall_kg": max(load_shortfall, 0.0),
            },
            "hospital": {
                "id": hospital["id"],
                "name": hospital["name"],
                "capability": hospital["capability"],
                "oxygen_supply": hospital["oxygen_supply"],
                "night_receiving": hospital["night_receiving"],
                "beds_total": hospital["beds_total"],
                "beds_available": hospital["beds_available"],
                "slots_total": hospital["slots_total"],
                "slots_available": hospital["slots_available"],
            },
            "aircraft": {
                "id": aircraft["id"],
                "aircraft_code": aircraft["aircraft_code"],
                "status": aircraft["status"],
                "night_qualified": aircraft["night_qualified"],
                "oxygen_kit": aircraft["oxygen_kit"],
            },
        }

    def evaluate_diversion(
        self,
        patient: Dict[str, Any],
        aircraft: Dict[str, Any],
        inputs: Dict[str, Any],
        target: Dict[str, Any],
        divert: Dict[str, Any],
    ) -> Dict[str, Any]:
        """改降核对：新医院收治条件/余量，以及剩余续航能否完成新航段。"""
        gaps: List[Dict[str, str]] = []
        if not divert["hospital_confirmed"]:
            gaps.append(_gap("hospital_unconfirmed", "改降医院%s收治条件未经电话确认" % target["name"]))
        gaps.extend(self._hospital_gaps(patient, inputs, target))

        # 已飞基地→接救点，剩余续航必须覆盖 接救点→新医院→新医院回基地
        remaining_endurance = round(aircraft["endurance_km"] - inputs["base_to_scene_km"], 1)
        needed_km = round(divert["scene_to_hospital_km"] + divert["hospital_to_base_km"] + RANGE_RESERVE_KM, 1)
        shortfall_km = round(needed_km - remaining_endurance, 1)
        if shortfall_km > 0:
            gaps.append(_gap("range", "改降航程需%skm，剩余续航%skm，缺口%skm" % (needed_km, remaining_endurance, shortfall_km)))

        return {
            "passed": not gaps,
            "gaps": gaps,
            "range": {
                "remaining_endurance_km": remaining_endurance,
                "required_km": needed_km,
                "shortfall_km": max(shortfall_km, 0.0),
            },
            "hospital": {
                "id": target["id"],
                "name": target["name"],
                "capability": target["capability"],
                "oxygen_supply": target["oxygen_supply"],
                "night_receiving": target["night_receiving"],
                "beds_available": target["beds_available"],
                "slots_available": target["slots_available"],
            },
        }
