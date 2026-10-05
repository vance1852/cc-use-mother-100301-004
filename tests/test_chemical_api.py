import unittest

from polar_station_foundation.api import route
from polar_station_foundation.chemical_service import ChemicalService
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database


class ChemicalApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.chemical = ChemicalService(self.database)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="站")
        self.service.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="ad",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="sft", actor_id="ad", new_actor_id="so",
                                    display_name="安全官", role="safety_officer",
                                    organization_id="o1")
        self.service.register_actor(request_id="opr", actor_id="ad", new_actor_id="op",
                                    display_name="库管", role="operator", organization_id="o1")
        self.service.register_site(request_id="sit", actor_id="op", site_id="s1",
                                   organization_id="o1", name="站", timezone_name="UTC")

    def _call(self, method, path, body=None, actor="op"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor},
                     chemical=self.chemical)

    def _prepare(self):
        self._call("POST", "/chemical/rule-books", {
            "request_id": "rb", "actor_id": "so", "site_id": "s1",
            "incompatible_pairs": [["acid", "base"]], "version": 1,
            "effective_at": "2026-01-01T00:00:00Z"}, actor="so")
        self._call("POST", "/chemical/units", {
            "request_id": "unt", "actor_id": "op", "site_id": "s1", "unit_id": "u1",
            "name": "舱", "barrier_level": 2}, actor="op")
        self._call("POST", "/chemical/locations", {
            "request_id": "loc", "actor_id": "op", "site_id": "s1", "location_id": "l1",
            "name": "柜", "unit_id": "u1", "capacity_total": 100, "quantity_unit": "L",
            "temp_min": -10, "temp_max": 10, "barrier_level": 2,
            "required_certifications": []})
        self._call("POST", "/chemical/safety-sheets", {
            "request_id": "sd", "actor_id": "so", "site_id": "s1", "supplier_name": "厂",
            "chemical_name": "酸", "batch_no": "B1", "version": 1, "hazard_class": "acid",
            "concentration": 99, "temp_min": -5, "temp_max": 5,
            "container_materials": ["glass"], "required_barrier_level": 2}, actor="so")
        sheet_id = dict(self.database.connection.execute(
            "SELECT sheet_id FROM safety_sheets WHERE batch_no='B1'").fetchone())["sheet_id"]
        return sheet_id

    def test_inbound_then_balances_over_http(self):
        sheet_id = self._prepare()
        status, payload = self._call("POST", "/chemical/inbound", {
            "request_id": "in", "actor_id": "op", "site_id": "s1", "supplier_name": "厂",
            "chemical_name": "酸", "batch_no": "B1", "sheet_id": sheet_id, "quantity": 40,
            "quantity_unit": "L", "expiry_date": "2027-01-01", "location_id": "l1",
            "container_material": "glass", "responsible_actor_id": "op"})
        self.assertEqual(201, status)
        status, payload = self._call("GET", "/chemical/stock-balances?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual(40.0, payload["items"][0]["ledger_remaining"])

    def test_rejected_review_returns_422_with_violations(self):
        sheet_id = self._prepare()
        status, payload = self._call("POST", "/chemical/inbound", {
            "request_id": "in", "actor_id": "op", "site_id": "s1", "supplier_name": "厂",
            "chemical_name": "酸", "batch_no": "B1", "sheet_id": sheet_id, "quantity": 40,
            "quantity_unit": "L", "expiry_date": "2027-01-01", "location_id": "l1",
            "container_material": "steel", "responsible_actor_id": "op"})
        self.assertEqual(422, status)
        self.assertEqual("chemical_review_rejected", payload["error"])
        self.assertTrue(payload["violations"])

    def test_snapshot_route_requires_at(self):
        status, payload = self._call("GET", "/chemical/locations/l1/snapshot")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_unknown_chemical_route_falls_through_to_404(self):
        status, payload = self._call("POST", "/chemical/nope", {})
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
