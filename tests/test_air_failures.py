import tempfile
import unittest
from pathlib import Path

from app import build_air_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


PATIENT = {"injury_severity": "urgent", "weight_kg": 80.0, "oxygen_lpm": 0.0, "pickup_location": "南坡落石点"}
AIRCRAFT = {"aircraft_code": "B-81MH", "base": "县体育场起降点", "slot": "2号位",
            "endurance_km": 500.0, "payload_capacity_kg": 300.0, "night_qualified": False, "oxygen_kit": True}
HOSPITAL = {"name": "市第二医院", "beds_total": 3, "helipad_slots_total": 1,
            "night_receiving": False, "oxygen_supply": True, "capability": "ALS"}
DISPATCH = {"weather_confirmed": True, "weather_flyable": True, "hospital_confirmed": True, "night_flight": False,
            "base_to_scene_km": 50.0, "scene_to_hospital_km": 40.0, "hospital_to_base_km": 30.0}


class AirFailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.air = build_air_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _resources(self):
        patient = self.air.register_patient(Actor("d1", "dispatcher"), "P-100", PATIENT)
        aircraft = self.air.register_aircraft(Actor("d1", "dispatcher"), AIRCRAFT)
        hospital = self.air.register_hospital(Actor("d1", "dispatcher"), HOSPITAL)
        return patient, aircraft, hospital

    def _mission_payload(self, patient, aircraft, hospital, **overrides):
        payload = dict(DISPATCH)
        payload.update({"patient_id": patient["id"], "aircraft_id": aircraft["id"], "hospital_id": hospital["id"]})
        payload.update(overrides)
        return payload

    def test_roles_enforced(self):
        with self.assertRaises(PermissionDenied):
            self.air.register_patient(Actor("x", "outsider"), "P-100", PATIENT)
        with self.assertRaises(PermissionDenied):
            self.air.register_aircraft(Actor("medic", "paramedic"), AIRCRAFT)
        with self.assertRaises(PermissionDenied):
            self.air.register_hospital(Actor("medic", "paramedic"), HOSPITAL)
        patient, aircraft, hospital = self._resources()
        with self.assertRaises(PermissionDenied):
            self.air.create_mission(Actor("m", "paramedic"), "M-1",
                                    self._mission_payload(patient, aircraft, hospital))
        mission = self.air.create_mission(Actor("d", "dispatcher"), "M-1",
                                          self._mission_payload(patient, aircraft, hospital))
        with self.assertRaises(PermissionDenied):
            self.air.act(Actor("c", "hospital_coordinator"), mission["id"], mission["version"], "takeoff", {})
        with self.assertRaises(PermissionDenied):
            self.air.act(Actor("p", "pilot"), mission["id"], mission["version"], "cancel", {"cancel_reason": "x"})

    def test_duplicate_references(self):
        self._resources()
        with self.assertRaises(Conflict):
            self.air.register_patient(Actor("d1", "dispatcher"), "P-100", PATIENT)
        with self.assertRaises(Conflict):
            self.air.register_aircraft(Actor("d1", "dispatcher"), AIRCRAFT)
        with self.assertRaises(Conflict):
            self.air.register_hospital(Actor("d1", "dispatcher"), HOSPITAL)

    def test_invalid_payloads(self):
        with self.assertRaises(ValidationError):
            self.air.register_patient(Actor("d1", "dispatcher"), "P-2", {**PATIENT, "weight_kg": "heavy"})
        patient, aircraft, hospital = self._resources()
        with self.assertRaises(ValidationError):
            self.air.create_mission(Actor("d", "dispatcher"), "M-2",
                                    {"patient_id": patient["id"], "aircraft_id": "one",
                                     "hospital_id": hospital["id"], **DISPATCH})
        with self.assertRaises(NotFound):
            self.air.create_mission(Actor("d", "dispatcher"), "M-3",
                                    self._mission_payload(patient, aircraft, hospital, hospital_id=999))

    def test_missing_entities(self):
        from src.domain import NotFound
        with self.assertRaises(NotFound):
            self.air.get_patient(Actor("d", "dispatcher"), 999)
        with self.assertRaises(NotFound):
            self.air.get_mission(Actor("d", "dispatcher"), 999)

    def test_stale_version_rejected_and_state_guarded(self):
        patient, aircraft, hospital = self._resources()
        mission = self.air.create_mission(Actor("d", "dispatcher"), "M-4",
                                          self._mission_payload(patient, aircraft, hospital))
        self.air.act(Actor("d", "dispatcher"), mission["id"], mission["version"], "recheck", DISPATCH)
        with self.assertRaises(Conflict):
            self.air.act(Actor("d", "dispatcher"), mission["id"], mission["version"], "recheck", DISPATCH)

    def test_illegal_transitions(self):
        patient, aircraft, hospital = self._resources()
        mission = self.air.create_mission(Actor("d", "dispatcher"), "M-5",
                                          self._mission_payload(patient, aircraft, hospital))
        with self.assertRaises(Conflict):
            self.air.act(Actor("p", "pilot"), mission["id"], mission["version"], "divert", {})
        with self.assertRaises(ValidationError):
            self.air.act(Actor("d", "dispatcher"), mission["id"], mission["version"], "cancel", {})

    def test_handover_requires_acceptance(self):
        patient, aircraft, hospital = self._resources()
        mission = self.air.create_mission(Actor("d", "dispatcher"), "M-6",
                                          self._mission_payload(patient, aircraft, hospital))
        mission = self.air.act(Actor("p", "pilot"), mission["id"], mission["version"], "takeoff", {})
        with self.assertRaises(ValidationError):
            self.air.act(Actor("c", "hospital_coordinator"), mission["id"], mission["version"], "handover",
                         {"handover_accepted": False})

    def test_duplicate_active_mission_for_patient(self):
        patient, aircraft, hospital = self._resources()
        self.air.create_mission(Actor("d", "dispatcher"), "M-7",
                                self._mission_payload(patient, aircraft, hospital))
        with self.assertRaises(Conflict):
            self.air.create_mission(Actor("d", "dispatcher"), "M-8",
                                    self._mission_payload(patient, aircraft, hospital))


if __name__ == "__main__":
    unittest.main()
