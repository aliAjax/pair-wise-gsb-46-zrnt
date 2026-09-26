"""空地转运规则：登记校验、派单核对、状态转换与载重/机位/床位计算。"""
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, ValidationError, boolean, choice, integer, number, optional_text, text


# 伤员状态：候派区 -> 已派单 -> 空中 -> 已交接；改降 / 取消为分支
PATIENT_WAITING = "waiting"
PATIENT_ASSIGNED = "assigned"
PATIENT_AIRBORNE = "airborne"
PATIENT_HANDED = "handed_over"
PATIENT_CANCELLED = "cancelled"

# 直升机状态：基地可用 -> 已派单(机位锁定) -> 空中(整机锁定) -> 返回后可用
CRAFT_AVAILABLE = "available"
CRAFT_RESERVED = "reserved"
CRAFT_AIRBORNE = "airborne"

HOSPITAL_ACTIVE = "active"

INJURY_LEVELS = ["critical", "urgent", "stable"]
RANGE_RESERVE_RATE = 0.9  # 往返里程需留出10%续航余量

CREATE_ROLES = {"dispatcher", "logistics", "hospital_coordinator"}
ACTION_ROLES = {
    "evaluate_dispatch": {"dispatcher"},
    "takeoff": {"dispatcher", "pilot"},
    "divert": {"dispatcher", "pilot", "paramedic"},
    "handover": {"paramedic", "hospital_coordinator"},
    "cancel": {"dispatcher", "paramedic"},
}
TRANSITIONS = {
    "takeoff": {PATIENT_ASSIGNED: PATIENT_AIRBORNE},
    "divert": {PATIENT_AIRBORNE: PATIENT_AIRBORNE},
    "handover": {PATIENT_AIRBORNE: PATIENT_HANDED},
    "cancel": {
        PATIENT_WAITING: PATIENT_CANCELLED,
        PATIENT_ASSIGNED: PATIENT_CANCELLED,
        PATIENT_AIRBORNE: PATIENT_CANCELLED,
    },
}

# 派单核对缺口代码与说明
GAP_MESSAGES = {
    "weather": "天气不满足目视飞行条件（电话确认未通过）",
    "night_unqualified": "夜间飞行但该机不具备夜航资质",
    "range": "往返里程超过续航余量（续航的90%）",
    "payload": "总重量超过直升机载重上限",
    "seat_full": "直升机机位已满",
    "oxygen": "目标医院不具备氧疗收治条件",
    "beds_full": "目标医院无余量机位",
    "weather_unconfirmed": "天气尚未经电话确认",
    "beds_unconfirmed": "医院余量尚未经电话确认",
    "craft_unavailable": "直升机当前不可派单",
    "hospital_inactive": "目标医院已停诊",
}


class AirRules:
    INITIAL_PATIENT_STATE = PATIENT_WAITING
    INITIAL_CRAFT_STATE = CRAFT_AVAILABLE
    INITIAL_HOSPITAL_STATE = HOSPITAL_ACTIVE

    # ---- 角色 ----

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    # ---- 登记校验 ----

    def validate_patient(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "name")
        choice(p, "injury_level", INJURY_LEVELS)
        number(p, "weight_kg", 0.1, 500)
        number(p, "oxygen_lpm", 0, 15)
        boolean(p, "oxygen_required", default=False)
        optional_text(p, "injury_note")
        optional_text(p, "pickup_location")
        if p["oxygen_required"] and p["oxygen_lpm"] <= 0:
            raise ValidationError("需氧伤员的需氧量必须大于0")
        return p

    def validate_helicopter(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "base")
        number(p, "range_km", 1)
        number(p, "payload_capacity_kg", 1)
        integer(p, "seats", 1, 50)
        boolean(p, "night_qualified", default=False)
        optional_text(p, "model")
        return p

    def validate_hospital(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "name")
        integer(p, "total_slots", 0)
        boolean(p, "oxygen_available", default=False)
        optional_text(p, "contact")
        return p

    # ---- 派生指标 ----

    @staticmethod
    def occupied_slots(all_patients: List[Dict[str, Any]], helicopter_id: int) -> int:
        """已锁定机位：已派单或空中、且仍占用该机的伤员数。"""
        return sum(
            1
            for item in all_patients
            if item["state"] in {PATIENT_ASSIGNED, PATIENT_AIRBORNE}
            and item["payload"].get("helicopter_id") == helicopter_id
        )

    @staticmethod
    def occupied_beds(all_patients: List[Dict[str, Any]], hospital_id: int) -> int:
        """医院已占用床位：已派单/空中的预留 + 已交接的实际占用。"""
        return sum(
            1
            for item in all_patients
            if item["payload"].get("hospital_id") == hospital_id
            and item["state"] in {PATIENT_ASSIGNED, PATIENT_AIRBORNE, PATIENT_HANDED}
        )

    @staticmethod
    def craft_state_after(craft: Dict[str, Any], patients: List[Dict[str, Any]]) -> str:
        linked = [
            p
            for p in patients
            if p["payload"].get("helicopter_id") == craft["id"]
            and p["state"] in {PATIENT_ASSIGNED, PATIENT_AIRBORNE}
        ]
        if any(p["state"] == PATIENT_AIRBORNE for p in linked):
            return CRAFT_AIRBORNE
        if linked:
            return CRAFT_RESERVED
        return CRAFT_AVAILABLE

    # ---- 派单核对输入 ----

    @staticmethod
    def _num(data: Dict[str, Any], key: str) -> float:
        value = data.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("%s必须是数字" % key)
        if float(value) < 0:
            raise ValidationError("%s不能小于0" % key)
        return float(value)

    def normalize_dispatch_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(data or {})
        return {
            "night_flight": boolean(d, "night_flight", default=False),
            "weather_ok": boolean(d, "weather_ok", default=False),
            "weather_phone_confirmed": boolean(d, "weather_phone_confirmed", default=False),
            "beds_phone_confirmed": boolean(d, "beds_phone_confirmed", default=False),
            "outbound_km": self._num(d, "outbound_km"),
            "return_km": self._num(d, "return_km"),
            "equipment_kg": self._num(d, "equipment_kg"),
            "crew_kg": self._num(d, "crew_kg"),
        }

    def assess_dispatch(
        self,
        patient: Dict[str, Any],
        craft: Dict[str, Any],
        hospital: Dict[str, Any],
        data: Dict[str, Any],
        all_patients: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
        """返回(缺口列表, 核算指标)。缺口为空表示可派单。"""
        if patient["state"] != PATIENT_WAITING:
            raise Conflict("伤员不在候派区")
        if craft["state"] == CRAFT_AIRBORNE:
            raise Conflict("直升机已起飞，整机锁定")

        cp, hp, pp = craft["payload"], hospital["payload"], patient["payload"]
        gaps: List[Dict[str, str]] = []

        def add_gap(code: str) -> None:
            gaps.append({"code": code, "message": GAP_MESSAGES[code]})

        # 天气（电话确认）
        if not data["weather_phone_confirmed"]:
            add_gap("weather_unconfirmed")
        elif not data["weather_ok"]:
            add_gap("weather")
        # 夜航资质
        if data["night_flight"] and not cp["night_qualified"]:
            add_gap("night_unqualified")
        # 往返里程 vs 续航余量
        round_trip = round(data["outbound_km"] + data["return_km"], 2)
        range_limit = round(float(cp["range_km"]) * RANGE_RESERVE_RATE, 2)
        if round_trip > range_limit:
            add_gap("range")
        # 载重：本架机上所有已锁定伤员 + 本次伤员 + 机组 + 设备/氧气
        locked_mass = sum(
            float(p["payload"]["weight_kg"])
            for p in all_patients
            if p["state"] in {PATIENT_ASSIGNED, PATIENT_AIRBORNE}
            and p["payload"].get("helicopter_id") == craft["id"]
        )
        total_mass = round(locked_mass + float(pp["weight_kg"]) + data["crew_kg"] + data["equipment_kg"], 2)
        if total_mass > float(cp["payload_capacity_kg"]):
            add_gap("payload")
        # 机位
        used_seats = self.occupied_slots(all_patients, craft["id"])
        if used_seats + 1 > int(cp["seats"]):
            add_gap("seat_full")
        # 医院状态与氧疗条件
        if hospital["state"] != HOSPITAL_ACTIVE:
            add_gap("hospital_inactive")
        else:
            if pp.get("oxygen_required") and not hp["oxygen_available"]:
                add_gap("oxygen")
            occupied = self.occupied_beds(all_patients, hospital["id"])
            remaining = int(hp["total_slots"]) - occupied
            if not data["beds_phone_confirmed"]:
                add_gap("beds_unconfirmed")
            elif remaining <= 0:
                add_gap("beds_full")
        if craft["state"] != CRAFT_AVAILABLE and craft["state"] != CRAFT_RESERVED:
            add_gap("craft_unavailable")

        metrics = {
            "round_trip_km": round_trip,
            "range_limit_km": range_limit,
            "total_payload_kg": total_mass,
            "payload_capacity_kg": float(cp["payload_capacity_kg"]),
            "seats_used_after": used_seats + 1,
            "seats_total": int(cp["seats"]),
            "hospital_remaining_after": int(hp["total_slots"]) - self.occupied_beds(all_patients, hospital["id"]) - 1,
        }
        return gaps, metrics

    # ---- 成功派单后的载荷 ----

    def apply_dispatch_success(self, patient: Dict[str, Any], craft: Dict[str, Any], hospital: Dict[str, Any], data: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(patient["payload"])
        p["helicopter_id"] = craft["id"]
        p["helicopter_code"] = craft.get("code", "")
        p["hospital_id"] = hospital["id"]
        p["hospital_name"] = hospital["payload"]["name"]
        p["dispatch"] = {
            "night_flight": data["night_flight"],
            "weather_ok": data["weather_ok"],
            "round_trip_km": metrics["round_trip_km"],
            "total_payload_kg": metrics["total_payload_kg"],
        }
        history = list(p.get("destination_history", []))
        history.append({"hospital_id": hospital["id"], "hospital_name": hospital["payload"]["name"], "stage": "assigned"})
        p["destination_history"] = history
        p.pop("last_gap", None)
        return p

    # ---- 状态转换 ----

    def require_transition(self, patient: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(patient["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def validate_takeoff(self, data: Dict[str, Any]) -> bool:
        confirmed = boolean(data or {}, "takeoff_confirmed", default=False)
        if not confirmed:
            raise ValidationError("起飞未确认")
        return True

    def validate_handover(self, data: Dict[str, Any]) -> bool:
        if not boolean(data or {}, "handover_accepted", default=False):
            raise ValidationError("医院尚未接收")
        return True

    def cancel_reason(self, data: Dict[str, Any]) -> str:
        return text(data or {}, "cancel_reason")

    def divert_payload(self, patient: Dict[str, Any], hospital: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(patient["payload"])
        if hospital["state"] != HOSPITAL_ACTIVE:
            raise Conflict("目标医院已停诊")
        if int(p.get("hospital_id")) == hospital["id"]:
            raise Conflict("改降医院与原医院相同")
        if p.get("oxygen_required") and not hospital["payload"]["oxygen_available"]:
            raise ValidationError("改降医院不具备氧疗收治条件")
        p["hospital_id"] = hospital["id"]
        p["hospital_name"] = hospital["payload"]["name"]
        history = list(p.get("destination_history", []))
        history.append({"hospital_id": hospital["id"], "hospital_name": hospital["payload"]["name"], "stage": "diverted"})
        p["destination_history"] = history
        return p
