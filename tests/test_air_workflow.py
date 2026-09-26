import tempfile
import unittest
from pathlib import Path

from app import build_air_service
from src.domain import Actor


PATIENT = {"injury_severity": "critical", "injury_note": "坠落伤，骨盆骨折", "weight_kg": 72.0,
           "oxygen_lpm": 6.0, "pickup_location": "青崖沟塌方点"}
PATIENT2 = {"injury_severity": "stable", "weight_kg": 60.0, "oxygen_lpm": 0.0, "pickup_location": "青崖沟塌方点"}
AIRCRAFT = {"aircraft_code": "B-70EM", "base": "县体育场起降点", "slot": "1号位",
            "endurance_km": 480.0, "payload_capacity_kg": 320.0, "night_qualified": True, "oxygen_kit": True}
HOSPITAL = {"name": "市人民医院", "beds_total": 12, "helipad_slots_total": 2,
            "night_receiving": True, "oxygen_supply": True, "capability": "ALS"}
HOSPITAL2 = {"name": "县中医医院", "beds_total": 5, "helipad_slots_total": 1,
             "night_receiving": True, "oxygen_supply": True, "capability": "ALS"}
DISPATCH = {"weather_confirmed": True, "weather_flyable": True, "weather_summary": "能见度8km，风速3级",
            "hospital_confirmed": True, "night_flight": False,
            "base_to_scene_km": 68.0, "scene_to_hospital_km": 45.0, "hospital_to_base_km": 22.0}

DISPATCHER = lambda: Actor("center", "dispatcher")
PILOT = lambda: Actor("pilot-1", "pilot")
COORD = lambda: Actor("coord-1", "hospital_coordinator")
MEDIC = lambda: Actor("medic-1", "paramedic")


class AirWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.air = build_air_service(str(Path(self.temp.name) / "test.db"))
        self.patient = self.air.register_patient(DISPATCHER(), "P-001", PATIENT)
        self.aircraft = self.air.register_aircraft(DISPATCHER(), AIRCRAFT)
        self.hospital = self.air.register_hospital(DISPATCHER(), HOSPITAL)

    def tearDown(self):
        self.temp.cleanup()

    def _dispatch(self, reference="M-001", data=None, actor=None):
        payload = dict(DISPATCH)
        payload.update(data or {})
        payload.update({"patient_id": self.patient["id"], "aircraft_id": self.aircraft["id"],
                        "hospital_id": self.hospital["id"]})
        return self.air.create_mission(actor or DISPATCHER(), reference, payload)

    def test_full_flow_locks_and_releases(self):
        mission = self._dispatch()
        self.assertEqual(mission["state"], "assigned")
        self.assertEqual(mission["payload"]["last_gaps"], [])

        # 起飞：直升机与机位锁定，医院床位/机位各预留1
        mission = self.air.act(PILOT(), mission["id"], mission["version"], "takeoff", {})
        self.assertEqual(mission["state"], "airborne")
        aircraft = self.air.get_aircraft(PILOT(), self.aircraft["id"])
        self.assertEqual(aircraft["status"], "locked")
        self.assertEqual(aircraft["locked_mission_id"], mission["id"])
        self.assertEqual(aircraft["locked_slot"], "市人民医院#slot")
        hospital = self.air.get_hospital(COORD(), self.hospital["id"])
        self.assertEqual(hospital["beds_available"], 11)
        self.assertEqual(hospital["slots_available"], 1)

        # 交接：预留床位转占用、机位释放、直升机解锁
        mission = self.air.act(COORD(), mission["id"], mission["version"], "handover",
                               {"handover_accepted": True, "handover_note": "入住ICU"})
        self.assertEqual(mission["state"], "closed")
        aircraft = self.air.get_aircraft(PILOT(), self.aircraft["id"])
        self.assertEqual(aircraft["status"], "available")
        self.assertIsNone(aircraft["locked_mission_id"])
        hospital = self.air.get_hospital(COORD(), self.hospital["id"])
        self.assertEqual(hospital["beds_available"], 11)
        self.assertEqual(hospital["beds_used"], 1)
        self.assertEqual(hospital["slots_available"], 2)

        patient = self.air.get_patient(MEDIC(), self.patient["id"])
        self.assertEqual(patient["state"], "delivered")
        self.assertEqual(patient["payload"]["delivered_hospital_name"], "市人民医院")

        # 时间线沿伤员可见：登记、派单、起飞、交接
        actions = [event["action"] for event in self.air.patient_timeline(DISPATCHER(), self.patient["id"])]
        self.assertEqual(actions, ["registered", "mission_created", "takeoff", "handover"])
        mission_actions = [event["action"] for event in self.air.mission_timeline(DISPATCHER(), mission["id"])]
        self.assertEqual(mission_actions, ["created", "takeoff", "handover"])

    def test_waiting_dispatch_and_recheck(self):
        mission = self._dispatch(data={"weather_confirmed": False})
        self.assertEqual(mission["state"], "waiting_dispatch")
        codes = [gap["code"] for gap in mission["payload"]["last_gaps"]]
        self.assertIn("weather_unconfirmed", codes)
        waiting = self.air.list_missions(DISPATCHER(), state="waiting_dispatch")
        self.assertEqual([item["id"] for item in waiting], [mission["id"]])

        # 电话确认天气后重新核对，进入可起飞
        mission = self.air.act(DISPATCHER(), mission["id"], mission["version"], "recheck", DISPATCH)
        self.assertEqual(mission["state"], "assigned")
        self.assertEqual(mission["payload"]["last_gaps"], [])
        self.assertEqual(mission["version"], 2)

    def test_diversion_releases_original_bed_first(self):
        hospital2 = self.air.register_hospital(DISPATCHER(), HOSPITAL2)
        mission = self._dispatch("M-002")
        mission = self.air.act(PILOT(), mission["id"], mission["version"], "takeoff", {})
        divert_data = {"hospital_id": hospital2["id"], "hospital_confirmed": True,
                       "scene_to_hospital_km": 30.0, "hospital_to_base_km": 55.0,
                       "reason": "原医院通报抢救位被占"}
        mission = self.air.act(DISPATCHER(), mission["id"], mission["version"], "divert", divert_data)
        self.assertEqual(mission["state"], "airborne")
        self.assertEqual(mission["hospital_id"], hospital2["id"])

        # 原医院预留已释放
        old = self.air.get_hospital(COORD(), self.hospital["id"])
        self.assertEqual(old["beds_available"], 12)
        self.assertEqual(old["slots_available"], 2)
        # 新医院已锁定
        new = self.air.get_hospital(COORD(), hospital2["id"])
        self.assertEqual(new["beds_available"], 4)
        self.assertEqual(new["slots_available"], 0)

        # 在新医院完成交接
        mission = self.air.act(COORD(), mission["id"], mission["version"], "handover", {"handover_accepted": True})
        self.assertEqual(mission["state"], "closed")
        patient = self.air.get_patient(MEDIC(), self.patient["id"])
        self.assertEqual(patient["payload"]["delivered_hospital_name"], "县中医医院")
        actions = [event["action"] for event in self.air.patient_timeline(DISPATCHER(), self.patient["id"])]
        self.assertEqual(actions, ["registered", "mission_created", "takeoff", "divert", "handover"])
        divert_event = [event for event in self.air.patient_timeline(DISPATCHER(), self.patient["id"]) if event["action"] == "divert"][0]
        self.assertIn("市人民医院", divert_event["details"]["summary"])
        self.assertIn("县中医医院", divert_event["details"]["summary"])

    def test_diversion_rejected_keeps_original_lock(self):
        hospital2 = self.air.register_hospital(DISPATCHER(), {"name": "无位医院", "beds_total": 0,
                                                              "helipad_slots_total": 0, "night_receiving": True,
                                                              "oxygen_supply": True, "capability": "ALS"})
        mission = self._dispatch("M-003")
        mission = self.air.act(PILOT(), mission["id"], mission["version"], "takeoff", {})
        from src.domain import ValidationError
        with self.assertRaises(ValidationError):
            self.air.act(DISPATCHER(), mission["id"], mission["version"], "divert",
                         {"hospital_id": hospital2["id"], "hospital_confirmed": True,
                          "scene_to_hospital_km": 30.0, "hospital_to_base_km": 55.0})
        # 任务仍 airborne，原医院仍预留，版本不变
        fresh = self.air.get_mission(DISPATCHER(), mission["id"])
        self.assertEqual(fresh["state"], "airborne")
        self.assertEqual(fresh["version"], mission["version"])
        old = self.air.get_hospital(COORD(), self.hospital["id"])
        self.assertEqual(old["beds_available"], 11)
        aircraft = self.air.get_aircraft(PILOT(), self.aircraft["id"])
        self.assertEqual(aircraft["status"], "locked")

    def test_takeoff_after_resource_conflict_returns_to_waiting(self):
        other_patient = self.air.register_patient(DISPATCHER(), "P-002", PATIENT2)
        first = self._dispatch("M-004")
        payload = dict(DISPATCH)
        payload.update({"patient_id": other_patient["id"], "aircraft_id": self.aircraft["id"],
                        "hospital_id": self.hospital["id"]})
        second = self.air.create_mission(DISPATCHER(), "M-005", payload)
        self.assertEqual(first["state"], "assigned")
        self.assertEqual(second["state"], "assigned")

        first = self.air.act(PILOT(), first["id"], first["version"], "takeoff", {})
        from src.domain import ValidationError
        with self.assertRaises(ValidationError):
            self.air.act(PILOT(), second["id"], second["version"], "takeoff", {})

        # 重新核对会暴露直升机已锁定的缺口，任务退回候派区
        second = self.air.act(DISPATCHER(), second["id"], second["version"], "recheck", DISPATCH)
        self.assertEqual(second["state"], "waiting_dispatch")
        self.assertIn("aircraft_locked", [gap["code"] for gap in second["payload"]["last_gaps"]])

    def test_cancel_waiting_releases_patient(self):
        mission = self._dispatch(data={"weather_confirmed": False})
        mission = self.air.act(DISPATCHER(), mission["id"], mission["version"], "cancel",
                               {"cancel_reason": "天气持续恶化，次日再议"})
        self.assertEqual(mission["state"], "cancelled")
        patient = self.air.get_patient(MEDIC(), self.patient["id"])
        self.assertEqual(patient["state"], "awaiting_dispatch")
        self.assertIsNone(patient["mission_id"])

    def test_stats_reflect_resources(self):
        mission = self._dispatch("M-006")
        self.air.act(PILOT(), mission["id"], mission["version"], "takeoff", {})
        stats = self.air.stats(DISPATCHER())
        self.assertEqual(stats["missions"]["airborne"], 1)
        self.assertEqual(stats["aircraft"]["locked"], 1)
        hospital_stat = [item for item in stats["hospitals"] if item["name"] == "市人民医院"][0]
        self.assertEqual(hospital_stat["beds_available"], 11)
        self.assertEqual(hospital_stat["slots_available"], 1)


if __name__ == "__main__":
    unittest.main()
