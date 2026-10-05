"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .chemical_service import ChemicalService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        chemical = ChemicalService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科考机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="站务负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-safety", actor_id="admin-001", new_actor_id="safety-001",
                               display_name="安全官", role="safety_officer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号科考站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="station_operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="station_operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # ---- 化学品入站与共储审查端到端链 ----
        chemical.publish_rule_book(request_id="req-rb1", actor_id="safety-001", site_id="site-001",
                                   incompatible_pairs=[["acid", "base"]], version=1,
                                   effective_at="2026-01-01T00:00:00Z")
        chemical.register_containment_unit(request_id="req-unit-a", actor_id="operator-001",
                                           site_id="site-001", unit_id="unit-a", name="酸类防泄漏舱",
                                           barrier_level=2)
        chemical.register_containment_unit(request_id="req-unit-b", actor_id="operator-001",
                                           site_id="site-001", unit_id="unit-b", name="碱类防泄漏舱",
                                           barrier_level=1)
        chemical.register_location(request_id="req-loc-a", actor_id="operator-001", site_id="site-001",
                                   location_id="loc-a", name="酸柜", unit_id="unit-a",
                                   capacity_total=100, quantity_unit="L", temp_min=-10, temp_max=10,
                                   barrier_level=2, required_certifications=["chem-handle"])
        chemical.register_location(request_id="req-loc-b", actor_id="operator-001", site_id="site-001",
                                   location_id="loc-b", name="碱柜", unit_id="unit-b",
                                   capacity_total=100, quantity_unit="L", temp_min=0, temp_max=30,
                                   barrier_level=1, required_certifications=[])
        chemical.grant_certification(request_id="req-cert", actor_id="safety-001",
                                     target_actor_id="operator-001", certification_type="chem-handle",
                                     valid_from="2026-01-01", valid_until="2026-12-31")
        # 同一供应商、不同批次、不同危险分类
        chemical.register_safety_sheet(request_id="req-sds-acid", actor_id="safety-001",
                                       site_id="site-001", supplier_name="极地试剂",
                                       chemical_name="冰醋酸", batch_no="BA-1", version=1,
                                       hazard_class="acid", concentration=99.5, temp_min=-5,
                                       temp_max=4, container_materials=["glass", "PE"],
                                       required_barrier_level=2)
        chemical.register_safety_sheet(request_id="req-sds-base", actor_id="safety-001",
                                       site_id="site-001", supplier_name="极地试剂",
                                       chemical_name="氢氧化钠", batch_no="NA-1", version=1,
                                       hazard_class="base", concentration=30, temp_min=5,
                                       temp_max=20, container_materials=["PE", "PP"],
                                       required_barrier_level=1)
        acid_sheet = dict(database.connection.execute(
            "SELECT sheet_id FROM safety_sheets WHERE batch_no='BA-1'").fetchone())["sheet_id"]
        base_sheet = dict(database.connection.execute(
            "SELECT sheet_id FROM safety_sheets WHERE batch_no='NA-1'").fetchone())["sheet_id"]

        inbound = chemical.inbound_chemical(
            request_id="req-inbound-acid", actor_id="operator-001", site_id="site-001",
            supplier_name="极地试剂", chemical_name="冰醋酸", batch_no="BA-1",
            sheet_id=acid_sheet, quantity=50, quantity_unit="L", expiry_date="2027-09-01",
            location_id="loc-a", container_material="glass",
            responsible_actor_id="operator-001", planned_quantity=10)
        inbound_replay = chemical.inbound_chemical(
            request_id="req-inbound-acid", actor_id="operator-001", site_id="site-001",
            supplier_name="极地试剂", chemical_name="冰醋酸", batch_no="BA-1",
            sheet_id=acid_sheet, quantity=50, quantity_unit="L", expiry_date="2027-09-01",
            location_id="loc-a", container_material="glass",
            responsible_actor_id="operator-001", planned_quantity=10)
        acid_batch = inbound.resource_id

        chemical.inbound_chemical(
            request_id="req-inbound-base", actor_id="operator-001", site_id="site-001",
            supplier_name="极地试剂", chemical_name="氢氧化钠", batch_no="NA-1",
            sheet_id=base_sheet, quantity=20, quantity_unit="L", expiry_date="2027-09-01",
            location_id="loc-b", container_material="PE",
            responsible_actor_id="operator-001")
        base_batch = dict(database.connection.execute(
            "SELECT batch_id FROM chemical_batches WHERE batch_no='NA-1'").fetchone())["batch_id"]
        base_placement = dict(database.connection.execute(
            "SELECT placement_id FROM chemical_placements WHERE batch_id=?",
            (base_batch,)).fetchone())["placement_id"]

        # 禁忌共储试评：碱想进酸舱，应被拒绝且不产生占用
        proposal = chemical.propose_placement(actor_id="operator-001", sheet_id=base_sheet,
                                              location_id="loc-a", quantity=5,
                                              container_material="PE",
                                              responsible_actor_id="operator-001",
                                              batch_id="trial")
        # 部分领用与盘点账实差异
        chemical.issue(request_id="req-issue", actor_id="operator-001",
                       placement_id=base_placement, quantity=8, reason="采样")
        count_receipt = chemical.count_stock(request_id="req-count", actor_id="operator-001",
                                             batch_id=base_batch, counted_quantity=10,
                                             note="账面12实物10")
        # 规则册升版，历史摆放保留旧版本
        chemical.publish_rule_book(request_id="req-rb2", actor_id="safety-001",
                                   site_id="site-001",
                                   incompatible_pairs=[["acid", "base"], ["oxidizer", "organic"]],
                                   version=2, effective_at="2026-09-20T00:00:00Z")
        snapshot = chemical.location_snapshot_at("loc-b", "2026-09-25T09:00:00Z")
        trace = chemical.batch_trace(acid_batch)
        # 泄漏隔离与解除
        isolation = chemical.impose_isolation(request_id="req-iso", actor_id="safety-001",
                                              site_id="site-001", unit_id="unit-a",
                                              reason="阀门渗漏演练", expires_at="2026-10-10")
        chemical.lift_isolation(request_id="req-iso-lift", actor_id="safety-001",
                                measure_id=isolation.resource_id)
        balances = chemical.stock_balances("site-001")
        acid_balance = next(item for item in balances if item["batch_id"] == acid_batch)
        base_balance = next(item for item in balances if item["batch_id"] == base_batch)

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "chemical": {
                      "inbound_replayed": inbound_replay.replayed,
                      "batch_count": 2,
                      "proposal_rejected": not proposal["accepted"],
                      "proposal_reason_is_coexistence": any("冲突" in v for v in proposal["violations"]),
                      "base_ledger_remaining": base_balance["ledger_remaining"],
                      "base_count_discrepancy": -2,
                      "acid_sheet_version_frozen": trace["placements"][0]["sheet_snapshot"]["version"],
                      "acid_rule_book_frozen": trace["placements"][0]["rule_book_snapshot"]["version"],
                      "snapshot_rule_version": snapshot["effective_rule_book_version"],
                      "acid_placed": acid_balance["placed_quantity"],
                  }}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
