import tempfile
import unittest
from pathlib import Path

from app import build_air_service
from src.air_service import AirService
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


PATIENT = {"name": "张三", "injury_level": "critical", "weight_kg": 80,
           "oxygen_required": True, "oxygen_lpm": 8, "pickup_location": "塌方点A"}
CRAFT = {"base": "县医院基地", "range_km": 300, "payload_capacity_kg": 600, "seats": 2, "night_qualified": True}
HOSPITAL = {"name": "市一院", "total_slots": 2, "oxygen_available": True}

GOOD_DISPATCH = {"night_flight": False, "weather_ok": True, "weather_phone_confirmed": True,
                 "beds_phone_confirmed": True, "outbound_km": 55, "return_km": 60,
                 "equipment_kg": 40, "crew_kg": 180}


class AirWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_air_service(str(Path(self.temp.name) / "air.db"))
        self.dispatcher = Actor("center", "dispatcher")
        self.pilot = Actor("pilot-1", "pilot")
        self.medic = Actor("medic-1", "paramedic")

    def tearDown(self):
        self.temp.cleanup()

    def _register(self, patient=PATIENT, craft=CRAFT, hospital=HOSPITAL, code_p="P-1", code_c="H-1", code_h="HOSP-1"):
        p = self.service.register_patient(self.dispatcher, code_p, patient)
        c = self.service.register_helicopter(self.dispatcher, code_c, craft)
        h = self.service.register_hospital(self.dispatcher, code_h, hospital)
        return p, c, h

    def test_full_flow_dispatch_takeoff_divert_handover(self):
        p1, c1, h1 = self._register()
        p2 = self.service.register_patient(self.dispatcher, "P-2", dict(PATIENT, name="李四"))
        h2 = self.service.register_hospital(self.dispatcher, "HOSP-2",
                                            {"name": "县中医院", "total_slots": 5, "oxygen_available": True})

        result = self.service.evaluate_dispatch(self.dispatcher, p1["id"], c1["id"], h1["id"], GOOD_DISPATCH)
        self.assertTrue(result["passed"])
        self.assertEqual(result["patient"]["state"], "assigned")
        self.assertEqual(result["helicopter"]["state"], "reserved")
        self.assertEqual(result["hospital"]["remaining_slots"], 1)

        # 第二人同机：机位2/2，床位占满
        result2 = self.service.evaluate_dispatch(self.dispatcher, p2["id"], c1["id"], h1["id"], GOOD_DISPATCH)
        self.assertTrue(result2["passed"])
        self.assertEqual(result2["helicopter"]["available_seats"], 0)

        # 第三人：候派区，缺口写明机位与床位
        p3 = self.service.register_patient(self.dispatcher, "P-3", dict(PATIENT, name="王五"))
        gap_result = self.service.evaluate_dispatch(self.dispatcher, p3["id"], c1["id"], h1["id"], GOOD_DISPATCH)
        self.assertFalse(gap_result["passed"])
        codes = {g["code"] for g in gap_result["gaps"]}
        self.assertIn("seat_full", codes)
        self.assertIn("beds_full", codes)
        p3_after = self.service.get_patient(self.dispatcher, p3["id"])
        self.assertEqual(p3_after["state"], "waiting")
        self.assertEqual(p3_after["payload"]["last_gap"]["gaps"], gap_result["gaps"])

        # 续航不足：往返150km，续航100km的90%=90km
        c2 = self.service.register_helicopter(self.dispatcher, "H-2",
                                              dict(CRAFT, range_km=100, payload_capacity_kg=1000))
        range_gap = self.service.evaluate_dispatch(self.dispatcher, p3["id"], c2["id"], h2["id"],
                                                   dict(GOOD_DISPATCH, outbound_km=80, return_km=70))
        self.assertFalse(range_gap["passed"])
        self.assertEqual([g["code"] for g in range_gap["gaps"]], ["range"])
        self.assertEqual(range_gap["metrics"]["range_limit_km"], 90)

        # 起飞：整机锁定，同机已派单伤员随机空中
        result = self.service.evaluate_dispatch(self.dispatcher, p3["id"], c2["id"], h2["id"],
                                                dict(GOOD_DISPATCH, outbound_km=40, return_km=40))
        # c2 对 p3 派单成功（往返80km在续航余量90km内；此前仅range缺口尝试，未锁定）
        self.assertTrue(result["passed"])
        p1_fresh = self.service.get_patient(self.dispatcher, p1["id"])
        airborne = self.service.takeoff(self.pilot, p1["id"], p1_fresh["version"], {"takeoff_confirmed": True})
        self.assertEqual(airborne["state"], "airborne")
        craft = self.service.get_helicopter(self.dispatcher, c1["id"])
        self.assertEqual(craft["state"], "airborne")
        self.assertEqual(self.service.get_patient(self.dispatcher, p2["id"])["state"], "airborne")

        # 起飞后不能再派单给该机
        p4 = self.service.register_patient(self.dispatcher, "P-4", dict(PATIENT, name="赵六"))
        with self.assertRaises(Conflict):
            self.service.evaluate_dispatch(self.dispatcher, p4["id"], c1["id"], h2["id"], GOOD_DISPATCH)

        # 改降：先释放原床位再锁定目标床位
        divert = self.service.divert(self.pilot, p1["id"], airborne["version"], h2["id"], "天气转差")
        self.assertEqual(divert["from_hospital"]["id"], h1["id"])
        self.assertEqual(divert["from_hospital"]["remaining_slots"], 1)  # 只剩P2占用
        self.assertEqual(divert["to_hospital"]["remaining_slots"], 3)    # P3已占h2一床，P1再占一床
        p1_after = divert["patient"]
        self.assertEqual(p1_after["payload"]["hospital_id"], h2["id"])
        self.assertEqual([h["stage"] for h in p1_after["payload"]["destination_history"]], ["assigned", "diverted"])

        # 交接P1：直升机仍因P2在空中
        handed1 = self.service.handover(self.medic, p1["id"], p1_after["version"], {"handover_accepted": True})
        self.assertEqual(handed1["patient"]["state"], "handed_over")
        self.assertEqual(handed1["helicopter"]["state"], "airborne")

        # 交接P2：全部落地，直升机释放
        p2_after = self.service.get_patient(self.dispatcher, p2["id"])
        handed2 = self.service.handover(self.medic, p2["id"], p2_after["version"], {"handover_accepted": True})
        self.assertEqual(handed2["patient"]["state"], "handed_over")
        self.assertEqual(handed2["helicopter"]["state"], "available")

        # 时间线沿伤员可见每次评估与去向
        timeline = self.service.patient_timeline(self.dispatcher, p1["id"])
        actions = [e["action"] for e in timeline]
        self.assertEqual(actions, ["registered", "dispatch_passed", "takeoff", "diverted", "handover"])
        self.assertEqual(timeline[3]["details"]["to_hospital_id"], h2["id"])

    def test_dispatch_gaps_weather_night_oxygen_payload_unconfirmed(self):
        p, c, h = self._register(code_p="PX", code_c="HX", code_h="HXH")
        # 电话未确认
        r = self.service.evaluate_dispatch(self.dispatcher, p["id"], c["id"], h["id"],
                                           {"outbound_km": 10, "return_km": 10})
        codes = {g["code"] for g in r["gaps"]}
        self.assertEqual(codes, {"weather_unconfirmed", "beds_unconfirmed"})
        # 天气确认但不满足
        r = self.service.evaluate_dispatch(self.dispatcher, p["id"], c["id"], h["id"],
                                           dict(GOOD_DISPATCH, weather_ok=False))
        self.assertIn("weather", {g["code"] for g in r["gaps"]})
        # 夜航无资质
        day_craft = self.service.register_helicopter(self.dispatcher, "HX-DAY",
                                                     dict(CRAFT, night_qualified=False))
        r = self.service.evaluate_dispatch(self.dispatcher, p["id"], day_craft["id"], h["id"],
                                           dict(GOOD_DISPATCH, night_flight=True))
        self.assertIn("night_unqualified", {g["code"] for g in r["gaps"]})
        # 医院无氧气
        no_o2 = self.service.register_hospital(self.dispatcher, "NOO2",
                                               {"name": "无氧气医院", "total_slots": 5, "oxygen_available": False})
        r = self.service.evaluate_dispatch(self.dispatcher, p["id"], c["id"], no_o2["id"], GOOD_DISPATCH)
        self.assertIn("oxygen", {g["code"] for g in r["gaps"]})
        # 超重（仅机组就超限）
        r = self.service.evaluate_dispatch(self.dispatcher, p["id"], c["id"], h["id"],
                                           dict(GOOD_DISPATCH, crew_kg=0, equipment_kg=521))
        self.assertIn("payload", {g["code"] for g in r["gaps"]})
        self.assertGreater(r["metrics"]["total_payload_kg"], 600)

    def test_cancel_releases_seat_and_bed(self):
        p, c, h = self._register(code_p="PC", code_c="HC", code_h="HC-H")
        r = self.service.evaluate_dispatch(self.dispatcher, p["id"], c["id"], h["id"], GOOD_DISPATCH)
        cancelled = self.service.cancel(self.dispatcher, p["id"], r["patient"]["version"],
                                        {"cancel_reason": "公路抢通，改救护车"})
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.service.get_helicopter(self.dispatcher, c["id"])["state"], "available")
        self.assertEqual(self.service.get_hospital(self.dispatcher, h["id"])["remaining_slots"], 2)

    def test_state_guard_and_validation(self):
        p, c, h = self._register(code_p="PG", code_c="HG", code_h="HG-H")
        # 未派单不能起飞
        with self.assertRaises(Conflict):
            self.service.takeoff(self.pilot, p["id"], p["version"], {"takeoff_confirmed": True})
        # 需氧量与伤情校验
        with self.assertRaises(ValidationError):
            self.service.register_patient(self.dispatcher, "BAD-1", dict(PATIENT, oxygen_lpm=0))
        with self.assertRaises(ValidationError):
            self.service.register_helicopter(self.dispatcher, "BAD-C", dict(CRAFT, seats=0))

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_patient(Actor("x", "outsider"), "NOPE", PATIENT)
        p, c, h = self._register(code_p="PP", code_c="HPP", code_h="PP-H")
        # 医护不能派单
        with self.assertRaises(PermissionDenied):
            self.service.evaluate_dispatch(self.medic, p["id"], c["id"], h["id"], GOOD_DISPATCH)
        # 调度员不能执行交接
        r = self.service.evaluate_dispatch(self.dispatcher, p["id"], c["id"], h["id"], GOOD_DISPATCH)
        self.service.takeoff(self.pilot, p["id"], r["patient"]["version"], {"takeoff_confirmed": True})
        with self.assertRaises(PermissionDenied):
            self.service.handover(self.dispatcher, p["id"], r["patient"]["version"] + 1,
                                  {"handover_accepted": True})

    def test_duplicate_code_and_overview(self):
        self._register()
        from src.domain import Conflict
        with self.assertRaises(Conflict):
            self.service.register_patient(self.dispatcher, "P-1", PATIENT)
        stats = self.service.overview(self.dispatcher)
        self.assertIn("patient", stats)
        self.assertEqual(stats["helicopter"]["available"], 1)
