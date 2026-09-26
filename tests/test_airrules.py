import unittest

from src.airrules import AirRules, CREW_AND_GEAR_KG, OXYGEN_CYLINDER_KG, RANGE_RESERVE_KM
from src.domain import ValidationError


PATIENT = {"injury_severity": "critical", "injury_note": "坠落伤，骨盆骨折", "weight_kg": 72.0,
           "oxygen_lpm": 6.0, "pickup_location": "青崖沟塌方点"}
AIRCRAFT = {"aircraft_code": "B-70EM", "base": "县体育场起降点", "slot": "1号位",
            "endurance_km": 480.0, "payload_capacity_kg": 320.0, "night_qualified": True, "oxygen_kit": True}
HOSPITAL = {"name": "市人民医院", "beds_total": 12, "helipad_slots_total": 2,
            "night_receiving": True, "oxygen_supply": True, "capability": "ALS"}
DISPATCH = {"weather_confirmed": True, "weather_flyable": True, "weather_summary": "能见度8km，风速3级",
            "hospital_confirmed": True, "night_flight": False,
            "base_to_scene_km": 68.0, "scene_to_hospital_km": 45.0, "hospital_to_base_km": 22.0}


class AirRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = AirRules()
        self.patient = self.rules.validate_patient(PATIENT)
        self.aircraft = {**self.rules.validate_aircraft(AIRCRAFT), "id": 1, "status": "available",
                         "locked_mission_id": None}
        self.hospital = {**self.rules.validate_hospital(HOSPITAL), "id": 2,
                         "beds_total": 12, "beds_available": 12,
                         "slots_total": 2, "slots_available": 2}

    def check(self, **overrides):
        inputs = dict(DISPATCH)
        inputs.update(overrides.pop("inputs", {}))
        aircraft = dict(self.aircraft)
        aircraft.update(overrides.pop("aircraft", {}))
        hospital = dict(self.hospital)
        hospital.update(overrides.pop("hospital", {}))
        return self.rules.evaluate_dispatch(self.patient, aircraft, hospital, inputs)

    def gap_codes(self, report):
        return [gap["code"] for gap in report["gaps"]]

    def test_all_checks_pass(self):
        report = self.check()
        self.assertTrue(report["passed"])
        self.assertEqual(report["gaps"], [])
        self.assertEqual(report["range"]["roundtrip_km"], 135.0)
        self.assertEqual(report["range"]["required_km"], 135.0 + RANGE_RESERVE_KM)
        self.assertEqual(report["load"]["required_kg"], 72.0 + OXYGEN_CYLINDER_KG + CREW_AND_GEAR_KG)

    def test_weather_must_be_confirmed_and_flyable(self):
        report = self.check(inputs={"weather_confirmed": False})
        self.assertIn("weather_unconfirmed", self.gap_codes(report))
        report = self.check(inputs={"weather_flyable": False})
        self.assertIn("weather_unfit", self.gap_codes(report))

    def test_night_qualification_gap(self):
        report = self.check(inputs={"night_flight": True}, aircraft={"night_qualified": False},
                            hospital={"night_receiving": True})
        self.assertIn("aircraft_night", self.gap_codes(report))
        report = self.check(inputs={"night_flight": True}, hospital={"night_receiving": False})
        self.assertIn("hospital_night", self.gap_codes(report))

    def test_range_gap_reports_shortfall(self):
        report = self.check(aircraft={"endurance_km": 120.0})
        self.assertFalse(report["passed"])
        self.assertIn("range", self.gap_codes(report))
        self.assertEqual(report["range"]["shortfall_km"], 135.0 + RANGE_RESERVE_KM - 120.0)

    def test_payload_gap_reports_shortfall(self):
        report = self.check(aircraft={"payload_capacity_kg": 260.0})
        self.assertIn("payload", self.gap_codes(report))
        self.assertEqual(report["load"]["shortfall_kg"], 284.0 - 260.0)

    def test_oxygen_kit_required(self):
        report = self.check(aircraft={"oxygen_kit": False})
        self.assertIn("aircraft_oxygen", self.gap_codes(report))

    def test_hospital_conditions(self):
        report = self.check(hospital={"beds_available": 0})
        self.assertIn("hospital_bed", self.gap_codes(report))
        report = self.check(hospital={"slots_available": 0})
        self.assertIn("hospital_slot", self.gap_codes(report))
        report = self.check(hospital={"capability": "BLS"})
        self.assertIn("hospital_capability", self.gap_codes(report))
        report = self.check(hospital={"oxygen_supply": False})
        self.assertIn("hospital_oxygen", self.gap_codes(report))
        report = self.check(inputs={"hospital_confirmed": False})
        self.assertIn("hospital_unconfirmed", self.gap_codes(report))

    def test_locked_aircraft_cannot_dispatch(self):
        report = self.check(aircraft={"status": "locked", "locked_mission_id": 9})
        self.assertIn("aircraft_locked", self.gap_codes(report))

    def test_diversion_range_check(self):
        inputs = dict(DISPATCH)
        divert = self.rules.validate_divert_input(
            {"hospital_id": 3, "hospital_confirmed": True,
             "scene_to_hospital_km": 120.0, "hospital_to_base_km": 260.0})
        target = {**self.hospital, "id": 3, "name": "第二医院"}
        report = self.rules.evaluate_diversion(self.patient, self.aircraft, inputs, target, divert)
        # 剩余续航 480-68=412；需 120+260+20=400，通过
        self.assertTrue(report["passed"])
        divert = self.rules.validate_divert_input(
            {"hospital_id": 3, "hospital_confirmed": True,
             "scene_to_hospital_km": 200.0, "hospital_to_base_km": 260.0})
        report = self.rules.evaluate_diversion(self.patient, self.aircraft, inputs, target, divert)
        self.assertIn("range", self.gap_codes(report))

    def test_invalid_registration(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_patient({**PATIENT, "weight_kg": -5})
        with self.assertRaises(ValidationError):
            self.rules.validate_aircraft({**AIRCRAFT, "endurance_km": 0})
        with self.assertRaises(ValidationError):
            self.rules.validate_hospital({**HOSPITAL, "capability": "XYZ"})


if __name__ == "__main__":
    unittest.main()
