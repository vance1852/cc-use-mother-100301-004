import unittest
from datetime import datetime, timedelta, timezone

from polar_station_foundation.chemical_service import ChemicalService
from polar_station_foundation.clock import FixedClock
from polar_station_foundation.errors import (
    ChemicalReviewError,
    ConflictError,
    PermissionDenied,
    ValidationError,
)
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database


class MovableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, days=0, hours=0):
        self.value += timedelta(days=days, hours=hours)


class ChemicalFixture:
    def __init__(self, clock=None):
        self.database = Database(":memory:")
        self.clock = clock or FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.svc = ChemicalService(self.database, self.clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="极地站务")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="so1", actor_id="ad", new_actor_id="so",
                                 display_name="安全官", role="safety_officer", organization_id="o1")
        self.base.register_actor(request_id="op1", actor_id="ad", new_actor_id="op",
                                 display_name="库管", role="operator", organization_id="o1")
        self.base.register_actor(request_id="rv1", actor_id="ad", new_actor_id="rv",
                                 display_name="复核员", role="reviewer", organization_id="o1")
        self.base.register_site(request_id="site", actor_id="op", site_id="s1",
                                organization_id="o1", name="一号站", timezone_name="UTC")
        self.svc.publish_rule_book(request_id="rb1", actor_id="so", site_id="s1",
                                   incompatible_pairs=[["acid", "base"]], version=1,
                                   effective_at="2026-01-01T00:00:00Z")
        self.svc.register_containment_unit(request_id="ua", actor_id="op", site_id="s1",
                                           unit_id="uA", name="A舱", barrier_level=2)
        self.svc.register_containment_unit(request_id="ub", actor_id="op", site_id="s1",
                                           unit_id="uB", name="B舱", barrier_level=2)
        self.svc.register_location(request_id="la", actor_id="op", site_id="s1",
                                   location_id="lA", name="酸柜", unit_id="uA",
                                   capacity_total=100, quantity_unit="L", temp_min=-10,
                                   temp_max=10, barrier_level=2,
                                   required_certifications=["chem-handle"])
        self.svc.register_location(request_id="lb", actor_id="op", site_id="s1",
                                   location_id="lB", name="碱柜", unit_id="uB",
                                   capacity_total=100, quantity_unit="L", temp_min=0,
                                   temp_max=30, barrier_level=1, required_certifications=[])
        self.svc.grant_certification(request_id="cert1", actor_id="so", target_actor_id="op",
                                     certification_type="chem-handle", valid_from="2026-01-01",
                                     valid_until="2026-12-31")

    def sheet(self, batch_no, hazard="acid", temp_min=-5, temp_max=5, materials=None,
              concentration=99.0, request_id=None, barrier=2, incompatible=None,
              chemical_name=None, supplier="极地试剂"):
        request_id = request_id or f"sds-{batch_no}"
        self.svc.register_safety_sheet(
            request_id=request_id, actor_id="so", site_id="s1", supplier_name=supplier,
            chemical_name=chemical_name or batch_no, batch_no=batch_no, version=1,
            hazard_class=hazard, concentration=concentration, temp_min=temp_min,
            temp_max=temp_max, container_materials=materials or ["glass", "PE"],
            incompatible_classes=incompatible or [], required_barrier_level=barrier)
        return dict(self.database.connection.execute(
            "SELECT sheet_id FROM safety_sheets WHERE site_id='s1' AND batch_no=?",
            (batch_no,)).fetchone())["sheet_id"]

    def batch_id(self, batch_no):
        return dict(self.database.connection.execute(
            "SELECT batch_id FROM chemical_batches WHERE batch_no=?", (batch_no,)).fetchone()
        )["batch_id"]

    def placement(self, batch_no):
        return dict(self.database.connection.execute(
            "SELECT placement_id FROM chemical_placements p JOIN chemical_batches b "
            "USING(batch_id) WHERE b.batch_no=? AND p.status IN ('occupied','quarantined') "
            "ORDER BY p.placed_at LIMIT 1", (batch_no,)).fetchone())["placement_id"]

    def inbound(self, request_id, batch_no, sheet, *, quantity=50, location="lA",
                material="glass", expiry="2027-01-01", actor="op", hazard_temp=None,
                planned=0.0, unit="L", chemical_name=None):
        return self.svc.inbound_chemical(
            request_id=request_id, actor_id=actor, site_id="s1", supplier_name="极地试剂",
            chemical_name=chemical_name or batch_no, batch_no=batch_no, sheet_id=sheet,
            quantity=quantity, quantity_unit=unit, expiry_date=expiry, location_id=location,
            container_material=material, responsible_actor_id="op", planned_quantity=planned)

    def close(self):
        self.database.close()


class InboundReviewTest(unittest.TestCase):
    def setUp(self):
        self.fx = ChemicalFixture()

    def tearDown(self):
        self.fx.close()

    def test_inbound_passes_full_review_and_freezes_snapshots(self):
        sheet = self.fx.sheet("BA-1")
        receipt = self.fx.inbound("in1", "BA-1", sheet)
        self.assertFalse(receipt.replayed)
        row = dict(self.fx.database.connection.execute(
            "SELECT * FROM chemical_placements WHERE batch_id=?",
            (receipt.resource_id,)).fetchone())
        self.assertEqual("occupied", row["status"])
        self.assertEqual(sheet, row["sheet_id"])
        rb = dict(self.fx.database.connection.execute(
            "SELECT version FROM rule_books WHERE rule_book_id=?",
            (row["rule_book_id"],)).fetchone())
        self.assertEqual(1, rb["version"])

    def test_rejected_inbound_leaves_no_occupancy_batch_or_ledger(self):
        sheet = self.fx.sheet("NA-1", hazard="base", temp_min=5, temp_max=25, barrier=1,
                              materials=["PE", "PP"])
        # 碱进 A 舱（酸所在同一防泄漏单元）：禁忌。先在 A 舱放酸
        acid_sheet = self.fx.sheet("BA-1")
        self.fx.inbound("acid", "BA-1", acid_sheet)
        with self.assertRaises(ChemicalReviewError) as caught:
            self.fx.inbound("base", "NA-1", sheet, location="lA", material="PE")
        self.assertTrue(any("禁忌" in v for v in caught.exception.violations))
        self.assertEqual(0, self.fx.database.connection.execute(
            "SELECT COUNT(*) c FROM chemical_placements WHERE batch_id IN "
            "(SELECT batch_id FROM chemical_batches WHERE batch_no='NA-1')").fetchone()["c"])
        self.assertEqual(0, self.fx.database.connection.execute(
            "SELECT COUNT(*) c FROM chemical_batches WHERE batch_no='NA-1'").fetchone()["c"])
        self.assertEqual(1, self.fx.database.connection.execute(
            "SELECT COUNT(*) c FROM stock_ledger").fetchone()["c"])

    def test_same_supplier_different_batches_may_have_different_hazard_class(self):
        acid = self.fx.sheet("BA-1", supplier="极地试剂")
        base = self.fx.sheet("NA-9", hazard="base", temp_min=5, temp_max=25, barrier=1,
                             materials=["PE"], supplier="极地试剂")
        self.fx.inbound("a", "BA-1", acid, location="lA")
        self.fx.inbound("b", "NA-9", base, location="lB", material="PE")
        classes = {r["hazard_class"] for r in self.fx.database.connection.execute(
            "SELECT DISTINCT hazard_class FROM safety_sheets WHERE supplier_name='极地试剂'")}
        self.assertEqual({"acid", "base"}, classes)

    def test_temperature_container_barrier_capacity_each_reject(self):
        sheet = self.fx.sheet("H-1", temp_min=-50, temp_max=-20)
        with self.assertRaises(ChemicalReviewError) as caught:
            self.fx.inbound("t", "H-1", sheet)
        self.assertTrue(any("温区" in v for v in caught.exception.violations))

        sheet2 = self.fx.sheet("H-2")
        with self.assertRaises(ChemicalReviewError) as caught:
            self.fx.inbound("m", "H-2", sheet2, material="unobtainium")
        self.assertTrue(any("容器材质" in v for v in caught.exception.violations))

        sheet3 = self.fx.sheet("H-3", barrier=5)
        with self.assertRaises(ChemicalReviewError) as caught:
            self.fx.inbound("b", "H-3", sheet3)
        self.assertTrue(any("屏障" in v for v in caught.exception.violations))

        sheet4 = self.fx.sheet("H-4")
        with self.assertRaises(ChemicalReviewError) as caught:
            self.fx.inbound("cap", "H-4", sheet4, quantity=999)
        self.assertTrue(any("容量" in v for v in caught.exception.violations))

    def test_missing_responsible_certification_rejects(self):
        sheet = self.fx.sheet("BA-1")
        self.fx.base.register_actor(request_id="op2", actor_id="ad", new_actor_id="op2",
                                    display_name="新人", role="operator", organization_id="o1")
        with self.assertRaises(ChemicalReviewError) as caught:
            self.svc_ = None
            self.fx.svc.inbound_chemical(
                request_id="xcert", actor_id="op", site_id="s1", supplier_name="极地试剂",
                chemical_name="BA-1", batch_no="BA-1", sheet_id=sheet, quantity=1,
                quantity_unit="L", expiry_date="2027-01-01", location_id="lA",
                container_material="glass", responsible_actor_id="op2")
        self.assertTrue(any("资格" in v for v in caught.exception.violations))

    def test_propose_is_read_only(self):
        sheet = self.fx.sheet("BA-1")
        result = self.fx.svc.propose_placement(
            actor_id="op", sheet_id=sheet, location_id="lA", quantity=10,
            container_material="glass", responsible_actor_id="op")
        self.assertTrue(result["accepted"])
        self.assertEqual(0, self.fx.database.connection.execute(
            "SELECT COUNT(*) c FROM chemical_placements").fetchone()["c"])


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.fx = ChemicalFixture()

    def tearDown(self):
        self.fx.close()

    def test_retry_returns_same_receipt_without_double_quantity(self):
        sheet = self.fx.sheet("BA-1")
        first = self.fx.inbound("in1", "BA-1", sheet, quantity=20)
        second = self.fx.inbound("in1", "BA-1", sheet, quantity=20)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        self.assertEqual(1, self.fx.database.connection.execute(
            "SELECT COUNT(*) c FROM chemical_batches").fetchone()["c"])
        self.assertEqual(20.0, self.fx.database.connection.execute(
            "SELECT SUM(quantity) s FROM chemical_placements").fetchone()["s"])
        self.assertEqual(1, self.fx.database.connection.execute(
            "SELECT COUNT(*) c FROM stock_ledger").fetchone()["c"])

    def test_same_request_id_changed_payload_conflicts(self):
        sheet = self.fx.sheet("BA-1")
        self.fx.inbound("in1", "BA-1", sheet, quantity=20)
        with self.assertRaises(ConflictError):
            self.fx.inbound("in1", "BA-1", sheet, quantity=21)


class MovementTest(unittest.TestCase):
    def setUp(self):
        self.fx = ChemicalFixture()
        self.sheet = self.fx.sheet("BA-1")
        self.fx.inbound("in1", "BA-1", self.sheet, quantity=50)

    def tearDown(self):
        self.fx.close()

    def test_partial_relocate_splits_placement_and_keeps_trace(self):
        source = self.fx.placement("BA-1")
        # 在 B 舱增加一个温区/屏障满足酸的库位（与 A 不同防泄漏单元）
        self.fx.svc.register_location(request_id="lc", actor_id="op", site_id="s1",
                                      location_id="lC", name="缓冲柜", unit_id="uB",
                                      capacity_total=100, quantity_unit="L", temp_min=-10,
                                      temp_max=10, barrier_level=2, required_certifications=[])
        receipt = self.fx.svc.relocate(request_id="mv1", actor_id="op",
                                       placement_id=source, target_location_id="lC",
                                       quantity=20, responsible_actor_id="op",
                                       container_material="glass")
        self.assertFalse(receipt.replayed)
        source_row = dict(self.fx.database.connection.execute(
            "SELECT quantity,status FROM chemical_placements WHERE placement_id=?",
            (source,)).fetchone())
        self.assertEqual(30.0, source_row["quantity"])
        move = dict(self.fx.database.connection.execute(
            "SELECT * FROM placement_moves WHERE to_placement_id=?",
            (receipt.resource_id,)).fetchone())
        self.assertEqual(20.0, move["quantity"])
        self.assertEqual(self.fx.batch_id("BA-1"), move["batch_id"])

    def test_relocate_into_incompatible_unit_rejects_atomically(self):
        source = self.fx.placement("BA-1")
        base_sheet = self.fx.sheet("NA-1", hazard="base", temp_min=0, temp_max=30, barrier=1,
                                   materials=["PE"])
        self.fx.inbound("in2", "NA-1", base_sheet, quantity=10, location="lB",
                        material="PE")
        with self.assertRaises(ChemicalReviewError):
            self.fx.svc.relocate(request_id="mvx", actor_id="op",
                                 placement_id=source, target_location_id="lB")
        self.assertEqual(50.0, dict(self.fx.database.connection.execute(
            "SELECT quantity FROM chemical_placements WHERE placement_id=?",
            (source,)).fetchone())["quantity"])

    def test_partial_issue_then_full_release(self):
        source = self.fx.placement("BA-1")
        self.fx.svc.issue(request_id="i1", actor_id="op", placement_id=source,
                          quantity=30, reason="采样")
        self.assertEqual(20.0, dict(self.fx.database.connection.execute(
            "SELECT quantity FROM chemical_placements WHERE placement_id=?",
            (source,)).fetchone())["quantity"])
        self.fx.svc.issue(request_id="i2", actor_id="op", placement_id=source,
                          quantity=20, reason="用完")
        self.assertEqual("released", dict(self.fx.database.connection.execute(
            "SELECT status FROM chemical_placements WHERE placement_id=?",
            (source,)).fetchone())["status"])
        balance = [b for b in self.fx.svc.stock_balances("s1")
                   if b["batch_no"] == "BA-1"][0]
        self.assertEqual(0.0, balance["ledger_remaining"])
        self.assertEqual("depleted", balance["status"])

    def test_loss_requires_reason_and_quarantine_blocks_consume(self):
        source = self.fx.placement("BA-1")
        with self.assertRaises(ValidationError):
            self.fx.svc.record_loss(request_id="l0", actor_id="op", placement_id=source,
                                    quantity=1, reason="  ")
        self.fx.svc.impose_isolation(request_id="iso", actor_id="so", site_id="s1",
                                     unit_id="uA", reason="泄漏")
        with self.assertRaises(ChemicalReviewError):
            self.fx.svc.issue(request_id="l1", actor_id="op", placement_id=source, quantity=1)

    def test_container_change_validates_against_batch_sheet(self):
        source = self.fx.placement("BA-1")
        with self.assertRaises(ChemicalReviewError):
            self.fx.svc.change_container(request_id="ccx", actor_id="op",
                                         placement_id=source, new_material="copper")
        self.fx.svc.change_container(request_id="cc1", actor_id="op",
                                    placement_id=source, new_material="PE", reason="破损更换")
        self.assertEqual("PE", dict(self.fx.database.connection.execute(
            "SELECT container_material FROM chemical_placements WHERE placement_id=?",
            (source,)).fetchone())["container_material"])
        trace = self.fx.svc.batch_trace(self.fx.batch_id("BA-1"))
        self.assertEqual(1, len(trace["container_changes"]))
        self.assertEqual("glass", trace["container_changes"][0]["old_material"])

    def test_return_to_storage_is_traced_to_original_batch(self):
        source = self.fx.placement("BA-1")
        self.fx.svc.issue(request_id="i1", actor_id="op", placement_id=source,
                          quantity=40, reason="外出采样")
        self.fx.svc.return_to_storage(
            request_id="r1", actor_id="op", batch_id=self.fx.batch_id("BA-1"),
            quantity=10, location_id="lA", container_material="glass",
            responsible_actor_id="op", reason="余料退回")
        balance = [b for b in self.fx.svc.stock_balances("s1")
                   if b["batch_no"] == "BA-1"][0]
        self.assertEqual(20.0, balance["ledger_remaining"])
        self.assertEqual(20.0, balance["placed_quantity"])
        self.assertEqual(10.0, balance["returned"])
        trace = self.fx.svc.batch_trace(self.fx.batch_id("BA-1"))
        occupied = [p for p in trace["placements"] if p["status"] == "occupied"]
        # 原摆放余量 10 + 退回产生的新摆放 10
        self.assertEqual(2, len(occupied))
        self.assertEqual(20.0, sum(p["quantity"] for p in occupied))
        returned = [p for p in occupied if p["placement_id"] != source]
        self.assertEqual(1, len(returned))
        self.assertEqual(self.sheet, returned[0]["sheet_id"])

    def test_cannot_return_more_than_ever_received(self):
        source = self.fx.placement("BA-1")
        with self.assertRaises(ValidationError):
            self.fx.svc.return_to_storage(
                request_id="rx", actor_id="op", batch_id=self.fx.batch_id("BA-1"),
                quantity=51, location_id="lA", container_material="glass",
                responsible_actor_id="op")


class VersioningAndSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.clock = MovableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.fx = ChemicalFixture(self.clock)
        self.sheet = self.fx.sheet("BA-1")
        self.fx.inbound("in1", "BA-1", self.sheet, quantity=50)

    def tearDown(self):
        self.fx.close()

    def test_new_rule_book_does_not_rewrite_existing_placement(self):
        self.fx.svc.publish_rule_book(
            request_id="rb2", actor_id="so", site_id="s1",
            incompatible_pairs=[["acid", "base"], ["organic", "oxidizer"]], version=2,
            effective_at="2026-10-02T00:00:00Z")
        trace = self.fx.svc.batch_trace(self.fx.batch_id("BA-1"))
        self.assertEqual(1, trace["placements"][0]["rule_book_snapshot"]["version"])

    def test_location_snapshot_reconstructs_rules_and_contents_at_point_in_time(self):
        source = self.fx.placement("BA-1")
        morning = self.fx.svc.location_snapshot_at("lA", "2026-10-01T09:00:00Z")
        self.assertEqual(1, morning["effective_rule_book_version"])
        self.assertEqual(1, len(morning["contents"]))
        self.assertEqual(50.0, morning["contents"][0]["quantity_at"])

        self.clock.advance(days=1)
        self.fx.svc.issue(request_id="i1", actor_id="op", placement_id=source,
                          quantity=20, reason="用")
        self.fx.svc.publish_rule_book(
            request_id="rb2", actor_id="so", site_id="s1",
            incompatible_pairs=[["acid", "base"]], version=2,
            effective_at="2026-10-02T03:00:00Z")
        self.fx.svc.update_location(
            request_id="lu1", actor_id="op", location_id="lA", capacity_total=80,
            temp_min=-20, temp_max=10, barrier_level=2, required_certifications=[])

        at = self.fx.svc.location_snapshot_at("lA", "2026-10-02T09:00:00Z")
        self.assertEqual(2, at["effective_rule_book_version"])
        self.assertEqual(2, at["config"]["version"])
        self.assertEqual(80, at["config"]["capacity_total"])
        self.assertEqual(30.0, at["contents"][0]["quantity_at"])

        early = self.fx.svc.location_snapshot_at("lA", "2026-10-01T09:00:00Z")
        self.assertEqual(1, early["config"]["version"])
        self.assertEqual(100, early["config"]["capacity_total"])
        self.assertEqual(50.0, early["contents"][0]["quantity_at"])


class IsolationExpiryAlertTest(unittest.TestCase):
    def setUp(self):
        self.clock = MovableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.fx = ChemicalFixture(self.clock)
        self.sheet = self.fx.sheet("BA-1")
        self.fx.inbound("in1", "BA-1", self.sheet, quantity=50)

    def tearDown(self):
        self.fx.close()

    def test_unit_isolation_quarantines_and_lift_restores(self):
        measure = self.fx.svc.impose_isolation(
            request_id="iso1", actor_id="so", site_id="s1", unit_id="uA",
            reason="法兰渗漏", expires_at="2026-10-03")
        self.assertEqual("quarantined", dict(self.fx.database.connection.execute(
            "SELECT status FROM chemical_placements WHERE placement_id=?",
            (self.fx.placement("BA-1"),)).fetchone())["status"])
        self.fx.svc.lift_isolation(request_id="lift1", actor_id="so",
                                   measure_id=measure.resource_id)
        self.assertEqual("occupied", dict(self.fx.database.connection.execute(
            "SELECT status FROM chemical_placements WHERE placement_id=?",
            (self.fx.placement("BA-1"),)).fetchone())["status"])

    def test_inbound_rejected_while_unit_isolated(self):
        self.fx.svc.impose_isolation(request_id="iso1", actor_id="so", site_id="s1",
                                     unit_id="uA", reason="泄漏")
        sheet2 = self.fx.sheet("BA-2")
        with self.assertRaises(ChemicalReviewError) as caught:
            self.fx.inbound("in2", "BA-2", sheet2)
        self.assertTrue(any("隔离" in v for v in caught.exception.violations))

    def test_expired_batch_quarantined_and_flagged_by_alert(self):
        sheet = self.fx.sheet("EX-1", hazard="base", temp_min=0, temp_max=30, barrier=1,
                             materials=["PE"])
        self.fx.inbound("ex", "EX-1", sheet, quantity=5, location="lB", material="PE",
                        expiry="2026-10-02")
        due = self.fx.svc.expiring_batches("s1", "2026-10-02")
        self.assertEqual(["EX-1"], [b["batch_no"] for b in due])
        self.clock.advance(days=2)
        self.fx.svc.mark_expired(actor_id="so", batch_id=self.fx.batch_id("EX-1"))
        self.assertEqual("quarantined", dict(self.fx.database.connection.execute(
            "SELECT status FROM chemical_placements WHERE placement_id=?",
            (self.fx.placement("EX-1"),)).fetchone())["status"])

    def test_certification_and_isolation_expiry_alerts(self):
        self.fx.svc.impose_isolation(request_id="iso1", actor_id="so", site_id="s1",
                                     location_id="lA", reason="检修",
                                     expires_at="2026-10-05")
        certs = self.fx.svc.expiring_certifications("s1", "2026-12-31")
        self.assertEqual(1, len(certs))
        due = self.fx.svc.isolation_due("s1", "2026-10-06")
        self.assertEqual(1, len(due))
        self.assertEqual([], self.fx.svc.isolation_due("s1", "2026-10-04"))


class StockBalanceTest(unittest.TestCase):
    def setUp(self):
        self.fx = ChemicalFixture()
        self.sheet = self.fx.sheet("BA-1")
        self.fx.inbound("in1", "BA-1", self.sheet, quantity=50)

    def tearDown(self):
        self.fx.close()

    def test_count_finds_book_to_actual_discrepancy(self):
        source = self.fx.placement("BA-1")
        self.fx.svc.issue(request_id="i1", actor_id="op", placement_id=source,
                          quantity=10, reason="用")
        self.fx.svc.record_loss(request_id="l1", actor_id="op", placement_id=source,
                                quantity=5, reason="蒸发")
        receipt = self.fx.svc.count_stock(request_id="cnt1", actor_id="rv",
                                          batch_id=self.fx.batch_id("BA-1"),
                                          counted_quantity=30, note="少5")
        self.assertEqual(-5.0, dict(self.fx.database.connection.execute(
            "SELECT discrepancy FROM stock_counts WHERE count_id=?",
            (receipt.resource_id,)).fetchone())["discrepancy"])
        balance = [b for b in self.fx.svc.stock_balances("s1")
                   if b["batch_no"] == "BA-1"][0]
        self.assertEqual(50, balance["received"])
        self.assertEqual(10, balance["issued"])
        self.assertEqual(5, balance["loss"])
        self.assertEqual(35.0, balance["ledger_remaining"])
        self.assertTrue(balance["account_mismatch"])

    def test_disposal_releases_all_occupancy(self):
        self.fx.svc.dispose_batch(request_id="d1", actor_id="so",
                                  batch_id=self.fx.batch_id("BA-1"), reason="变质销毁")
        balance = [b for b in self.fx.svc.stock_balances("s1")
                   if b["batch_no"] == "BA-1"][0]
        self.assertEqual(0.0, balance["ledger_remaining"])
        self.assertEqual("disposed", balance["status"])
        self.assertEqual(0, self.fx.database.connection.execute(
            "SELECT COUNT(*) c FROM chemical_placements WHERE batch_id=? AND status!='released'",
            (self.fx.batch_id("BA-1"),)).fetchone()["c"])


class PermissionTest(unittest.TestCase):
    def setUp(self):
        self.fx = ChemicalFixture()

    def tearDown(self):
        self.fx.close()

    def test_only_safety_officer_publishes_rules(self):
        with self.assertRaises(PermissionDenied):
            self.fx.svc.publish_rule_book(request_id="x", actor_id="op", site_id="s1",
                                          incompatible_pairs=[], version=9)

    def test_operator_cannot_dispose(self):
        sheet = self.fx.sheet("BA-1")
        self.fx.inbound("in1", "BA-1", sheet)
        with self.assertRaises(PermissionDenied):
            self.fx.svc.dispose_batch(request_id="d", actor_id="op",
                                      batch_id=self.fx.batch_id("BA-1"), reason="x")


if __name__ == "__main__":
    unittest.main()
