"""化学品入站与共储审查事务服务。

设计要点：

* 每次摆放（chemical_placements）都冻结当时的 SDS 版本（sheet_id）与规则册
  版本（rule_book_id）。规则册或 SDS 更新只产生新版本，绝不回写已发生的摆放，
  因此任意时点的审查依据都可以原样还原。
* 所有写操作都在单个 BEGIN IMMEDIATE 事务内完成审查与落库：审查失败则整体
  回滚，不留下任何占用；request_receipts 保证重试返回同一回执，数量不会重复
  增减。
* 泄漏隔离、部分领用、退库、过期、更换容器都以 batch_id 为主线串联，可回溯
  到原批次与其 SDS 版本。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Iterable

from . import chemical as rules
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (
    ChemicalReviewError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .storage import Database


SAFETY_ROLES = ("admin", "safety_officer")
OPERATOR_ROLES = ("admin", "operator")
MOVEMENT_TYPES = {"receive": 1, "issue": -1, "loss": -1, "return": 1, "disposal": -1}


class ChemicalService:
    """提供化学品主数据、审查入库、流转台账与追溯查询。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _require_actor(self, connection, actor_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = dict(row)
        if not actor["active"]:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require_role(self, actor: dict[str, Any], allowed: Iterable[str]) -> None:
        if actor["role"] not in allowed:
            raise PermissionDenied("当前角色不能执行该动作")

    def _same_site(self, actor: dict[str, Any], site: dict[str, Any]) -> None:
        if actor["organization_id"] != site["organization_id"] and actor["role"] != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    def _get_site(self, connection, site_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return dict(row)

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        from .models import WriteReceipt

        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    # ------------------------------------------------------------ 主数据登记

    def register_containment_unit(self, *, request_id: str, actor_id: str, site_id: str,
                                  unit_id: str, name: str, barrier_level: int = 0) -> Any:
        payload = {"actor_id": actor_id, "site_id": site_id, "unit_id": unit_id,
                   "name": name, "barrier_level": barrier_level}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, OPERATOR_ROLES)
            self._same_site(actor, self._get_site(connection, site_id))
            if not isinstance(barrier_level, int) or barrier_level < 0:
                raise ValidationError("barrier_level 必须是非负整数")
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO containment_units(unit_id,site_id,name,barrier_level,active,"
                        "created_by,created_at) VALUES(?,?,?,?,1,?,?)",
                        (unit_id, site_id, name, barrier_level, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("防泄漏单元编号或名称已存在") from exc
                self._audit(connection, actor_id=actor_id, action="chemical.unit_registered",
                            resource_type="containment_unit", resource_id=unit_id,
                            detail={"site_id": site_id, "name": name, "barrier_level": barrier_level})
                return "containment_unit", unit_id, {"unit_id": unit_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_containment_unit", payload=payload, create=create)

    def register_location(self, *, request_id: str, actor_id: str, site_id: str, location_id: str,
                          name: str, unit_id: str, capacity_total: float, quantity_unit: str,
                          temp_min: float, temp_max: float, barrier_level: int = 0,
                          required_certifications: list[str] | None = None) -> Any:
        required_certifications = required_certifications or []
        payload = {"actor_id": actor_id, "site_id": site_id, "location_id": location_id, "name": name,
                   "unit_id": unit_id, "capacity_total": capacity_total, "quantity_unit": quantity_unit,
                   "temp_min": temp_min, "temp_max": temp_max, "barrier_level": barrier_level,
                   "required_certifications": required_certifications}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, OPERATOR_ROLES)
            site = self._get_site(connection, site_id)
            self._same_site(actor, site)
            unit = self._get_unit(connection, unit_id, site_id)
            self._validate_location_config(capacity_total, temp_min, temp_max, barrier_level,
                                           required_certifications, unit)

            def create():
                now = self._now()
                config = {"name": name, "unit_id": unit_id, "capacity_total": capacity_total,
                          "quantity_unit": quantity_unit, "temp_min": temp_min, "temp_max": temp_max,
                          "barrier_level": barrier_level,
                          "required_certifications": list(required_certifications)}
                try:
                    connection.execute(
                        "INSERT INTO storage_locations(location_id,site_id,name,unit_id,capacity_total,"
                        "quantity_unit,temp_min,temp_max,barrier_level,required_certifications_json,"
                        "version,active,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,1,?,?)",
                        (location_id, site_id, name, unit_id, capacity_total, quantity_unit,
                         temp_min, temp_max, barrier_level, canonical_json(required_certifications),
                         actor_id, now),
                    )
                    connection.execute(
                        "INSERT INTO storage_location_history(history_id,location_id,version,"
                        "config_json,changed_at,changed_by) VALUES(?,?,1,?,?,?)",
                        (uuid.uuid4().hex, location_id, canonical_json(config), now, actor_id),
                    )
                except Exception as exc:
                    raise ConflictError("库位编号或名称已存在，或防泄漏单元无效") from exc
                self._audit(connection, actor_id=actor_id, action="chemical.location_registered",
                            resource_type="storage_location", resource_id=location_id,
                            detail={"site_id": site_id, "unit_id": unit_id, "config_hash": digest(config)})
                return "storage_location", location_id, {"location_id": location_id, "version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_location", payload=payload, create=create)

    def update_location(self, *, request_id: str, actor_id: str, location_id: str,
                        capacity_total: float, temp_min: float, temp_max: float,
                        barrier_level: int, required_certifications: list[str]) -> Any:
        """登记库位配置的新版本；历史摆放与历史配置均保留，不做回写。"""

        payload = {"actor_id": actor_id, "location_id": location_id, "capacity_total": capacity_total,
                   "temp_min": temp_min, "temp_max": temp_max, "barrier_level": barrier_level,
                   "required_certifications": required_certifications}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, OPERATOR_ROLES)
            location = self._get_location(connection, location_id)
            site = self._get_site(connection, location["site_id"])
            self._same_site(actor, site)
            unit = self._get_unit(connection, location["unit_id"], location["site_id"])
            self._validate_location_config(capacity_total, temp_min, temp_max, barrier_level,
                                           required_certifications, unit)

            def create():
                now = self._now()
                new_version = int(location["version"]) + 1
                config = {"name": location["name"], "unit_id": location["unit_id"],
                          "capacity_total": capacity_total, "quantity_unit": location["quantity_unit"],
                          "temp_min": temp_min, "temp_max": temp_max, "barrier_level": barrier_level,
                          "required_certifications": list(required_certifications)}
                connection.execute(
                    "UPDATE storage_locations SET capacity_total=?,temp_min=?,temp_max=?,"
                    "barrier_level=?,required_certifications_json=?,version=? WHERE location_id=?",
                    (capacity_total, temp_min, temp_max, barrier_level,
                     canonical_json(required_certifications), new_version, location_id),
                )
                connection.execute(
                    "INSERT INTO storage_location_history(history_id,location_id,version,"
                    "config_json,changed_at,changed_by) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, location_id, new_version, canonical_json(config), now, actor_id),
                )
                self._audit(connection, actor_id=actor_id, action="chemical.location_updated",
                            resource_type="storage_location", resource_id=location_id,
                            detail={"version": new_version, "config_hash": digest(config)})
                return "storage_location", location_id, {"location_id": location_id, "version": new_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="update_location", payload=payload, create=create)

    def _validate_location_config(self, capacity_total, temp_min, temp_max, barrier_level,
                                  required_certifications, unit) -> None:
        if not isinstance(capacity_total, (int, float)) or capacity_total <= 0:
            raise ValidationError("capacity_total 必须为正数")
        if not isinstance(temp_min, (int, float)) or not isinstance(temp_max, (int, float)):
            raise ValidationError("温区边界必须是数字")
        if temp_min > temp_max:
            raise ValidationError("temp_min 不能大于 temp_max")
        if not isinstance(barrier_level, int) or barrier_level < 0:
            raise ValidationError("barrier_level 必须是非负整数")
        if not isinstance(required_certifications, list) or not all(
            isinstance(item, str) and item.strip() for item in required_certifications
        ):
            raise ValidationError("required_certifications 必须是非空字符串列表")

    def register_safety_sheet(self, *, request_id: str, actor_id: str, site_id: str,
                              supplier_name: str, chemical_name: str, batch_no: str, version: int,
                              hazard_class: str, concentration: float, temp_min: float,
                              temp_max: float, container_materials: list[str],
                              incompatible_classes: list[str] | None = None,
                              required_barrier_level: int = 0, effective_at: str | None = None,
                              sheet_id: str | None = None) -> Any:
        """登记某供应商某批次化学品 SDS 的一个版本。

        同一供应商名称下不同批次可以拥有不同危险分类，批次与分类以
        (supplier_name, batch_no, version) 为准，不以供应商名称推断。
        """

        incompatible_classes = incompatible_classes or []
        sheet_id = sheet_id or uuid.uuid4().hex
        payload = {"actor_id": actor_id, "site_id": site_id, "supplier_name": supplier_name,
                   "chemical_name": chemical_name, "batch_no": batch_no, "version": version,
                   "hazard_class": hazard_class, "concentration": concentration,
                   "temp_min": temp_min, "temp_max": temp_max,
                   "container_materials": container_materials,
                   "incompatible_classes": incompatible_classes,
                   "required_barrier_level": required_barrier_level,
                   "effective_at": effective_at or self._now()}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, SAFETY_ROLES)
            self._same_site(actor, self._get_site(connection, site_id))
            if not isinstance(version, int) or version < 1:
                raise ValidationError("version 必须是不小于 1 的整数")
            if not isinstance(concentration, (int, float)) or not 0 < concentration <= 100:
                raise ValidationError("concentration 必须在 (0, 100] 范围内")
            if temp_min > temp_max:
                raise ValidationError("temp_min 不能大于 temp_max")
            if not container_materials or not all(
                isinstance(item, str) and item.strip() for item in container_materials
            ):
                raise ValidationError("container_materials 必须是非空字符串列表")
            if not all(isinstance(item, str) and item.strip() for item in incompatible_classes):
                raise ValidationError("incompatible_classes 必须是字符串列表")
            if not isinstance(required_barrier_level, int) or required_barrier_level < 0:
                raise ValidationError("required_barrier_level 必须是非负整数")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO safety_sheets(sheet_id,site_id,supplier_name,chemical_name,"
                        "batch_no,version,hazard_class,concentration,temp_min,temp_max,"
                        "container_materials_json,incompatible_classes_json,required_barrier_level,"
                        "effective_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sheet_id, site_id, supplier_name.strip(), chemical_name.strip(),
                         batch_no.strip(), version, hazard_class.strip(), concentration,
                         temp_min, temp_max, canonical_json(container_materials),
                         canonical_json(incompatible_classes), required_barrier_level,
                         payload["effective_at"], actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该供应商/批次的 SDS 版本已存在") from exc
                self._audit(connection, actor_id=actor_id, action="chemical.sheet_registered",
                            resource_type="safety_sheet", resource_id=sheet_id,
                            detail={"site_id": site_id, "supplier_name": supplier_name,
                                    "batch_no": batch_no, "version": version,
                                    "hazard_class": hazard_class, "concentration": concentration})
                return "safety_sheet", sheet_id, {"sheet_id": sheet_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_safety_sheet", payload=payload, create=create)

    def publish_rule_book(self, *, request_id: str, actor_id: str, site_id: str,
                          incompatible_pairs: list[list[str]], version: int,
                          effective_at: str | None = None, rule_book_id: str | None = None) -> Any:
        """发布一版全站共储规则册；新版本不影响引用旧版本的历史摆放。"""

        rule_book_id = rule_book_id or uuid.uuid4().hex
        effective_at = effective_at or self._now()
        payload = {"actor_id": actor_id, "site_id": site_id, "version": version,
                   "incompatible_pairs": incompatible_pairs, "effective_at": effective_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, SAFETY_ROLES)
            self._same_site(actor, self._get_site(connection, site_id))
            try:
                rules.normalize_pairs(incompatible_pairs)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            def create():
                try:
                    connection.execute(
                        "INSERT INTO rule_books(rule_book_id,site_id,version,incompatible_pairs_json,"
                        "effective_at,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (rule_book_id, site_id, version, canonical_json(incompatible_pairs),
                         effective_at, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该场所的规则册版本已存在") from exc
                self._audit(connection, actor_id=actor_id, action="chemical.rule_book_published",
                            resource_type="rule_book", resource_id=rule_book_id,
                            detail={"site_id": site_id, "version": version,
                                    "pairs": len(incompatible_pairs)})
                return "rule_book", rule_book_id, {"rule_book_id": rule_book_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_rule_book", payload=payload, create=create)

    def grant_certification(self, *, request_id: str, actor_id: str, target_actor_id: str,
                            certification_type: str, valid_from: str, valid_until: str) -> Any:
        payload = {"actor_id": actor_id, "target_actor_id": target_actor_id,
                   "certification_type": certification_type, "valid_from": valid_from,
                   "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, SAFETY_ROLES)
            if valid_from > valid_until:
                raise ValidationError("valid_from 不能晚于 valid_until")
            if connection.execute("SELECT 1 FROM actors WHERE actor_id=?",
                                  (target_actor_id,)).fetchone() is None:
                raise NotFoundError("被授权人不存在")

            def create():
                certification_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO certifications(certification_id,actor_id,certification_type,"
                        "valid_from,valid_until,active,granted_by,created_at) VALUES(?,?,?,?,?,1,?,?)",
                        (certification_id, target_actor_id, certification_type.strip(),
                         valid_from, valid_until, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资格记录冲突") from exc
                self._audit(connection, actor_id=actor_id, action="chemical.certification_granted",
                            resource_type="certification", resource_id=certification_id,
                            detail={"target_actor_id": target_actor_id,
                                    "certification_type": certification_type,
                                    "valid_until": valid_until})
                return "certification", certification_id, {"certification_id": certification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="grant_certification", payload=payload, create=create)

    # -------------------------------------------------------------- 快照读取

    def _get_unit(self, connection, unit_id: str, site_id: str | None = None) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM containment_units WHERE unit_id=?",
                                 (unit_id,)).fetchone()
        if row is None:
            raise NotFoundError("防泄漏单元不存在")
        unit = dict(row)
        if site_id is not None and unit["site_id"] != site_id:
            raise ValidationError("库位与防泄漏单元不属于同一站点")
        if not unit["active"]:
            raise ValidationError("防泄漏单元已停用")
        return unit

    def _get_location(self, connection, location_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM storage_locations WHERE location_id=?",
                                 (location_id,)).fetchone()
        if row is None:
            raise NotFoundError("库位不存在")
        location = dict(row)
        if not location["active"]:
            raise ValidationError("库位已停用")
        return location

    def _get_sheet(self, connection, sheet_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM safety_sheets WHERE sheet_id=?",
                                 (sheet_id,)).fetchone()
        if row is None:
            raise NotFoundError("SDS 版本不存在")
        return self._sheet_snapshot(row)

    def _sheet_snapshot(self, row) -> dict[str, Any]:
        return {"sheet_id": row["sheet_id"], "site_id": row["site_id"],
                "supplier_name": row["supplier_name"], "chemical_name": row["chemical_name"],
                "batch_no": row["batch_no"], "version": row["version"],
                "hazard_class": row["hazard_class"], "concentration": row["concentration"],
                "temp_min": row["temp_min"], "temp_max": row["temp_max"],
                "container_materials": json.loads(row["container_materials_json"]),
                "incompatible_classes": json.loads(row["incompatible_classes_json"]),
                "required_barrier_level": row["required_barrier_level"],
                "effective_at": row["effective_at"]}

    def _effective_rule_book(self, connection, site_id: str, at: str | None = None) -> dict[str, Any]:
        at = at or self._now()
        row = connection.execute(
            "SELECT * FROM rule_books WHERE site_id=? AND effective_at<=? "
            "ORDER BY version DESC LIMIT 1", (site_id, at),
        ).fetchone()
        if row is None:
            raise ValidationError("该场所尚无生效的共储规则册")
        return {"rule_book_id": row["rule_book_id"], "version": row["version"],
                "effective_at": row["effective_at"],
                "pairs": rules.normalize_pairs(json.loads(row["incompatible_pairs_json"]))}

    def _location_config_at(self, connection, location_id: str, at: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM storage_location_history WHERE location_id=? AND changed_at<=? "
            "ORDER BY version DESC LIMIT 1", (location_id, at),
        ).fetchone()
        if row is None:
            raise NotFoundError("该时点库位配置尚不存在")
        config = json.loads(row["config_json"])
        config["location_id"] = location_id
        config["version"] = row["version"]
        return config

    def _held_certifications(self, connection, actor_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT certification_type,valid_from,valid_until,active FROM certifications "
            "WHERE actor_id=?", (actor_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _active_isolation(self, connection, *, site_id: str, unit_id: str | None = None,
                          location_id: str | None = None, batch_id: str | None = None,
                          at: str | None = None) -> list[dict[str, Any]]:
        at = at or self._now()
        clauses = ["site_id=?", "started_at<=?", "(lifted_at IS NULL)",
                   "(expires_at IS NULL OR expires_at>?)"]
        parameters: list[Any] = [site_id, at, at]
        scope: list[dict[str, Any]] = []
        row = connection.execute(
            "SELECT * FROM isolation_measures WHERE " + " AND ".join(clauses)
            + " ORDER BY started_at", parameters,
        ).fetchall()
        for item in row:
            item = dict(item)
            if unit_id is not None and item["unit_id"] not in (None, unit_id):
                continue
            if location_id is not None and item["location_id"] not in (None, location_id):
                continue
            if batch_id is not None and item["batch_id"] not in (None, batch_id):
                continue
            scope.append(item)
        return scope

    def _unit_occupants(self, connection, unit_id: str, exclude_batch_id: str | None = None
                        ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT p.batch_id, s.hazard_class, s.incompatible_classes_json "
            "FROM chemical_placements p JOIN safety_sheets s ON s.sheet_id=p.sheet_id "
            "WHERE p.unit_id=? AND p.status IN ('occupied','quarantined')",
            (unit_id,),
        ).fetchall()
        occupants: list[dict[str, Any]] = []
        for row in rows:
            if exclude_batch_id is not None and row["batch_id"] == exclude_batch_id:
                continue
            occupants.append({"batch_id": row["batch_id"], "hazard_class": row["hazard_class"],
                              "incompatible_classes": json.loads(row["incompatible_classes_json"])})
        return occupants

    # -------------------------------------------------------------- 审查核心

    def _review_placement(self, connection, *, site_id: str, sheet: dict[str, Any],
                          location: dict[str, Any], unit: dict[str, Any], container_material: str,
                          quantity: float, responsible_actor_id: str, batch_id: str,
                          rule_book: dict[str, Any] | None = None,
                          at: str | None = None) -> dict[str, Any]:
        """对一次拟议摆放执行全部审查，返回所用快照；违规则抛出且不写库。"""

        at = at or self._now()
        violations: list[str] = []
        candidate_sheet = {**sheet, "batch_id": batch_id}
        violations += rules.temperature_violations(sheet, location)
        violations += rules.container_violations(container_material, sheet)
        violations += rules.barrier_violations(sheet, location, unit)

        required_certs = location.get("required_certifications", [])
        if "required_certifications_json" in location:
            required_certs = json.loads(location["required_certifications_json"])
        held = self._held_certifications(connection, responsible_actor_id)
        violations += rules.certification_violations(required_certs, held, at[:10])

        rule_book = rule_book or self._effective_rule_book(connection, site_id, at)
        occupants = self._unit_occupants(connection, unit["unit_id"], exclude_batch_id=batch_id)
        violations += rules.coexistence_violations(candidate_sheet, occupants, rule_book["pairs"])

        occupied = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS used FROM chemical_placements "
            "WHERE location_id=? AND status IN ('occupied','quarantined')",
            (location["location_id"],),
        ).fetchone()["used"]
        if occupied + quantity > float(location["capacity_total"]) + 1e-9:
            violations.append(
                f"库位容量不足：已占用 {occupied}，本次 {quantity}，"
                f"容量 {location['capacity_total']}"
            )

        for measure in self._active_isolation(connection, site_id=site_id,
                                              unit_id=unit["unit_id"],
                                              location_id=location["location_id"],
                                              batch_id=batch_id, at=at):
            violations.append(f"处于有效隔离措施 {measure['measure_id']} 之下：{measure['reason']}")

        if violations:
            raise ChemicalReviewError(violations)
        return {"rule_book": rule_book}

    def propose_placement(self, *, actor_id: str, sheet_id: str, location_id: str,
                          quantity: float, container_material: str,
                          responsible_actor_id: str, batch_id: str | None = None) -> dict[str, Any]:
        """只读试评：返回审查结论而不产生任何占用。"""

        with self.database.transaction() as connection:
            self._require_actor(connection, actor_id)
            sheet = self._get_sheet(connection, sheet_id)
            location = self._get_location(connection, location_id)
            unit = self._get_unit(connection, location["unit_id"], location["site_id"])
            try:
                self._review_placement(
                    connection, site_id=location["site_id"], sheet=sheet, location=location,
                    unit=unit, container_material=container_material, quantity=quantity,
                    responsible_actor_id=responsible_actor_id,
                    batch_id=batch_id or f"proposal-{uuid.uuid4().hex}",
                )
            except ChemicalReviewError as exc:
                return {"accepted": False, "violations": exc.violations}
            return {"accepted": True, "violations": []}

    # ------------------------------------------------------------ 入库与流转

    def inbound_chemical(self, *, request_id: str, actor_id: str, site_id: str, supplier_name: str,
                         chemical_name: str, batch_no: str, sheet_id: str, quantity: float,
                         quantity_unit: str, expiry_date: str, location_id: str,
                         container_material: str, responsible_actor_id: str,
                         planned_quantity: float = 0.0) -> Any:
        """一次完整入库：登记批次、通过审查、锁定库位并记入台账，否则全不发生。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "supplier_name": supplier_name,
                   "batch_no": batch_no, "sheet_id": sheet_id, "quantity": quantity,
                   "location_id": location_id, "container_material": container_material,
                   "responsible_actor_id": responsible_actor_id,
                   "planned_quantity": planned_quantity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, OPERATOR_ROLES)
            site = self._get_site(connection, site_id)
            self._same_site(actor, site)
            sheet = self._get_sheet(connection, sheet_id)
            if sheet["site_id"] != site_id:
                raise ValidationError("SDS 与入库场所不匹配")
            if (sheet["supplier_name"], sheet["batch_no"]) != (supplier_name.strip(), batch_no.strip()):
                raise ValidationError("SDS 的供应商与批次必须与入库实物一致")
            if not isinstance(quantity, (int, float)) or quantity <= 0:
                raise ValidationError("quantity 必须为正数")
            if not isinstance(planned_quantity, (int, float)) or planned_quantity < 0:
                raise ValidationError("planned_quantity 不能为负")
            if planned_quantity > quantity:
                raise ValidationError("计划用量不能超过入库数量")
            if expiry_date < self._today():
                raise ValidationError("不能入库已过期批次")
            location = self._get_location(connection, location_id)
            if location["site_id"] != site_id:
                raise ValidationError("库位与入库场所不匹配")
            if location["quantity_unit"] != quantity_unit:
                raise ValidationError("计量单位与库位定义不一致")
            unit = self._get_unit(connection, location["unit_id"], site_id)
            review = self._review_placement(
                connection, site_id=site_id, sheet=sheet, location=location, unit=unit,
                container_material=container_material, quantity=quantity,
                responsible_actor_id=responsible_actor_id, batch_id=f"new:{supplier_name}:{batch_no}",
            )

            def create():
                now = self._now()
                batch_id = uuid.uuid4().hex
                placement_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO chemical_batches(batch_id,site_id,supplier_name,chemical_name,"
                        "batch_no,sheet_id,quantity_received,quantity_unit,expiry_date,status,"
                        "received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?)",
                        (batch_id, site_id, sheet["supplier_name"], sheet["chemical_name"],
                         sheet["batch_no"], sheet_id, quantity, quantity_unit, expiry_date,
                         now, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("该供应商同批次化学品已经入库") from exc
                connection.execute(
                    "INSERT INTO chemical_placements(placement_id,batch_id,location_id,unit_id,"
                    "initial_quantity,quantity,planned_quantity,quantity_unit,container_material,"
                    "sheet_id,rule_book_id,responsible_actor_id,status,placed_at,request_id,"
                    "created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'occupied',?,?,?)",
                    (placement_id, batch_id, location_id, unit["unit_id"], quantity, quantity,
                     planned_quantity, quantity_unit, container_material, sheet_id,
                     review["rule_book"]["rule_book_id"], responsible_actor_id, now, request_id,
                     actor_id),
                )
                self._status_event(connection, placement_id=placement_id, old_status=None,
                                   new_status="occupied", actor_id=actor_id, at=now,
                                   request_id=request_id, reason="inbound")
                self._insert_ledger(connection, batch_id=batch_id, movement_type="receive",
                                    quantity=quantity, quantity_unit=quantity_unit,
                                    placement_id=placement_id, actor_id=actor_id,
                                    request_id=request_id, at=now)
                self._audit(connection, actor_id=actor_id, action="chemical.inbound",
                            resource_type="chemical_batch", resource_id=batch_id,
                            detail={"placement_id": placement_id, "location_id": location_id,
                                    "unit_id": unit["unit_id"], "quantity": quantity,
                                    "sheet_id": sheet_id, "sheet_version": sheet["version"],
                                    "rule_book_id": review["rule_book"]["rule_book_id"],
                                    "hazard_class": sheet["hazard_class"]})
                return "chemical_batch", batch_id, {"batch_id": batch_id,
                                                     "placement_id": placement_id}

            return self._idempotent(connection, request_id=request_id, action="inbound_chemical",
                                    payload=payload, create=create)

    def relocate(self, *, request_id: str, actor_id: str, placement_id: str,
                 target_location_id: str, quantity: float | None = None,
                 responsible_actor_id: str | None = None, container_material: str | None = None
                 ) -> Any:
        """整体或部分移位；目标库位重新审查，源摆放与新摆放在同一事务内切换。"""

        payload = {"actor_id": actor_id, "placement_id": placement_id,
                   "target_location_id": target_location_id, "quantity": quantity,
                   "responsible_actor_id": responsible_actor_id,
                   "container_material": container_material}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, OPERATOR_ROLES)
            source = self._get_placement(connection, placement_id)
            if source["status"] == "released":
                raise ValidationError("不能移动已经释放的摆放")
            if source["status"] == "quarantined":
                raise ChemicalReviewError(["该摆放处于隔离状态，解除隔离前不得移位"])
            batch = self._get_batch(connection, source["batch_id"])
            self._same_site(actor, self._get_site(connection, batch["site_id"]))
            move_quantity = float(source["quantity"]) if quantity is None else quantity
            if move_quantity <= 0 or move_quantity > source["quantity"] + 1e-9:
                raise ValidationError("移位数量必须在源摆放现存数量范围内")
            target = self._get_location(connection, target_location_id)
            if target["site_id"] != batch["site_id"]:
                raise ValidationError("目标库位与批次不属于同一站点")
            if target_location_id == source["location_id"]:
                raise ValidationError("目标库位与源库位相同，无需移位")
            target_unit = self._get_unit(connection, target["unit_id"], batch["site_id"])
            sheet = self._get_sheet(connection, source["sheet_id"])
            material = container_material or source["container_material"]
            responsible = responsible_actor_id or source["responsible_actor_id"]
            review = self._review_placement(
                connection, site_id=batch["site_id"], sheet=sheet, location=target,
                unit=target_unit, container_material=material, quantity=move_quantity,
                responsible_actor_id=responsible, batch_id=batch["batch_id"],
            )

            def create():
                now = self._now()
                new_placement_id = uuid.uuid4().hex
                move_id = uuid.uuid4().hex
                rule_book_id = review["rule_book"]["rule_book_id"]
                connection.execute(
                    "INSERT INTO chemical_placements(placement_id,batch_id,location_id,unit_id,"
                    "initial_quantity,quantity,planned_quantity,quantity_unit,container_material,"
                    "sheet_id,rule_book_id,responsible_actor_id,status,placed_at,request_id,"
                    "created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'occupied',?,?,?)",
                    (new_placement_id, batch["batch_id"], target_location_id,
                     target_unit["unit_id"], move_quantity, move_quantity, 0,
                     source["quantity_unit"], material, source["sheet_id"], rule_book_id,
                     responsible, now, request_id, actor_id),
                )
                self._status_event(connection, placement_id=new_placement_id, old_status=None,
                                   new_status="occupied", actor_id=actor_id, at=now,
                                   request_id=request_id, reason="relocate")
                self._reduce_placement(connection, source, move_quantity, actor_id, request_id)
                connection.execute(
                    "INSERT INTO placement_moves(move_id,batch_id,from_placement_id,"
                    "to_placement_id,quantity,actor_id,moved_at,request_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (move_id, batch["batch_id"], placement_id, new_placement_id,
                     move_quantity, actor_id, now, request_id),
                )
                self._audit(connection, actor_id=actor_id, action="chemical.relocated",
                            resource_type="chemical_batch", resource_id=batch["batch_id"],
                            detail={"from_placement_id": placement_id,
                                    "to_placement_id": new_placement_id,
                                    "quantity": move_quantity,
                                    "from_location_id": source["location_id"],
                                    "to_location_id": target_location_id})
                return "placement", new_placement_id, {"placement_id": new_placement_id,
                                                        "move_id": move_id}

            return self._idempotent(connection, request_id=request_id, action="relocate",
                                    payload=payload, create=create)

    def issue(self, *, request_id: str, actor_id: str, placement_id: str, quantity: float,
              reason: str = "") -> Any:
        """部分领用：扣减摆放与台账，余量为零时释放库位占用。"""

        return self._consume(request_id=request_id, actor_id=actor_id, placement_id=placement_id,
                             quantity=quantity, reason=reason, movement_type="issue",
                             action_name="chemical.issued", action="issue")

    def record_loss(self, *, request_id: str, actor_id: str, placement_id: str, quantity: float,
                    reason: str) -> Any:
        """登记损耗并释放对应库位占用。"""

        if not reason.strip():
            raise ValidationError("损耗必须说明原因")
        return self._consume(request_id=request_id, actor_id=actor_id, placement_id=placement_id,
                             quantity=quantity, reason=reason, movement_type="loss",
                             action_name="chemical.loss_recorded", action="record_loss")

    def _consume(self, *, request_id: str, actor_id: str, placement_id: str, quantity: float,
                 reason: str, movement_type: str, action_name: str, action: str) -> Any:
        payload = {"actor_id": actor_id, "placement_id": placement_id, "quantity": quantity,
                   "reason": reason, "movement_type": movement_type}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, OPERATOR_ROLES)
            placement = self._get_placement(connection, placement_id)
            if placement["status"] == "released":
                raise ValidationError("摆放已经释放")
            if placement["status"] == "quarantined":
                raise ChemicalReviewError(["摆放处于隔离状态，不得领用或核销"])
            batch = self._get_batch(connection, placement["batch_id"])
            self._same_site(actor, self._get_site(connection, batch["site_id"]))
            if quantity <= 0 or quantity > placement["quantity"] + 1e-9:
                raise ValidationError("数量必须在摆放现存数量范围内")
            if self._active_isolation(connection, site_id=batch["site_id"],
                                      batch_id=batch["batch_id"]):
                raise ChemicalReviewError(["批次处于隔离状态，不得领用或核销"])

            def create():
                now = self._now()
                self._reduce_placement(connection, placement, quantity, actor_id, request_id,
                                       reason=movement_type)
                ledger_id = self._insert_ledger(
                    connection, batch_id=batch["batch_id"], movement_type=movement_type,
                    quantity=quantity, quantity_unit=placement["quantity_unit"],
                    placement_id=placement_id, actor_id=actor_id, request_id=request_id, at=now,
                    reason=reason,
                )
                self._refresh_batch_status(connection, batch["batch_id"], now, actor_id)
                self._audit(connection, actor_id=actor_id, action=action_name,
                            resource_type="chemical_batch", resource_id=batch["batch_id"],
                            detail={"placement_id": placement_id, "quantity": quantity,
                                    "movement_type": movement_type, "reason": reason})
                return "stock_ledger", ledger_id, {"ledger_id": ledger_id,
                                                   "placement_id": placement_id}

            return self._idempotent(connection, request_id=request_id, action=action,
                                    payload=payload, create=create)

    def return_to_storage(self, *, request_id: str, actor_id: str, batch_id: str, quantity: float,
                          location_id: str, container_material: str, responsible_actor_id: str,
                          reason: str = "") -> Any:
        """退库：把已领出的实物按原批次重新审查后放回库位。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "quantity": quantity,
                   "location_id": location_id, "container_material": container_material,
                   "responsible_actor_id": responsible_actor_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, OPERATOR_ROLES)
            batch = self._get_batch(connection, batch_id)
            self._same_site(actor, self._get_site(connection, batch["site_id"]))
            if batch["status"] == "disposed":
                raise ValidationError("已销毁批次不能退库")
            if quantity <= 0:
                raise ValidationError("quantity 必须为正数")
            if self.ledger_remaining(connection, batch_id) + quantity > batch["quantity_received"] + 1e-9:
                raise ValidationError("退库后数量超过该批次累计入库量")
            if batch["expiry_date"] < self._today():
                raise ChemicalReviewError([f"批次已于 {batch['expiry_date']} 过期，不能退回复用库位"])
            sheet = self._get_sheet(connection, batch["sheet_id"])
            location = self._get_location(connection, location_id)
            if location["site_id"] != batch["site_id"]:
                raise ValidationError("库位与批次不属于同一站点")
            unit = self._get_unit(connection, location["unit_id"], batch["site_id"])
            review = self._review_placement(
                connection, site_id=batch["site_id"], sheet=sheet, location=location, unit=unit,
                container_material=container_material, quantity=quantity,
                responsible_actor_id=responsible_actor_id, batch_id=batch_id,
            )

            def create():
                now = self._now()
                placement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO chemical_placements(placement_id,batch_id,location_id,unit_id,"
                    "initial_quantity,quantity,planned_quantity,quantity_unit,container_material,"
                    "sheet_id,rule_book_id,responsible_actor_id,status,placed_at,request_id,"
                    "created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'occupied',?,?,?)",
                    (placement_id, batch_id, location_id, unit["unit_id"], quantity, quantity, 0,
                     batch["quantity_unit"], container_material, batch["sheet_id"],
                     review["rule_book"]["rule_book_id"], responsible_actor_id, now, request_id,
                     actor_id),
                )
                self._status_event(connection, placement_id=placement_id, old_status=None,
                                   new_status="occupied", actor_id=actor_id, at=now,
                                   request_id=request_id, reason="return")
                ledger_id = self._insert_ledger(
                    connection, batch_id=batch_id, movement_type="return", quantity=quantity,
                    quantity_unit=batch["quantity_unit"], placement_id=placement_id,
                    actor_id=actor_id, request_id=request_id, at=now, reason=reason,
                )
                if batch["status"] != "active":
                    connection.execute(
                        "UPDATE chemical_batches SET status='active' WHERE batch_id=?", (batch_id,)
                    )
                self._audit(connection, actor_id=actor_id, action="chemical.returned",
                            resource_type="chemical_batch", resource_id=batch_id,
                            detail={"placement_id": placement_id, "quantity": quantity,
                                    "location_id": location_id})
                return "stock_ledger", ledger_id, {"ledger_id": ledger_id,
                                                   "placement_id": placement_id}

            return self._idempotent(connection, request_id=request_id, action="return_to_storage",
                                    payload=payload, create=create)

    def dispose_batch(self, *, request_id: str, actor_id: str, batch_id: str, reason: str) -> Any:
        """销毁批次全部剩余库存并释放其全部占用。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, SAFETY_ROLES)
            batch = self._get_batch(connection, batch_id)
            self._same_site(actor, self._get_site(connection, batch["site_id"]))
            if not reason.strip():
                raise ValidationError("销毁必须说明原因")
            placements = connection.execute(
                "SELECT * FROM chemical_placements WHERE batch_id=? AND status IN "
                "('occupied','quarantined')", (batch_id,),
            ).fetchall()
            remaining = sum(row["quantity"] for row in placements)
            if remaining <= 0:
                raise ValidationError("该批次已无在库数量")

            def create():
                now = self._now()
                ledger_id = None
                for row in placements:
                    row = dict(row)
                    quantity = row["quantity"]
                    connection.execute(
                        "UPDATE chemical_placements SET status='released',released_at=?,"
                        "quantity=0 WHERE placement_id=?", (now, row["placement_id"]),
                    )
                    self._status_event(connection, placement_id=row["placement_id"],
                                       old_status=row["status"], new_status="released",
                                       actor_id=actor_id, at=now, request_id=request_id,
                                       reason="disposal")
                    ledger_id = self._insert_ledger(
                        connection, batch_id=batch_id, movement_type="disposal",
                        quantity=quantity, quantity_unit=batch["quantity_unit"],
                        placement_id=row["placement_id"], actor_id=actor_id,
                        request_id=request_id, at=now, reason=reason,
                    )
                connection.execute(
                    "UPDATE chemical_batches SET status='disposed' WHERE batch_id=?", (batch_id,)
                )
                self._audit(connection, actor_id=actor_id, action="chemical.disposed",
                            resource_type="chemical_batch", resource_id=batch_id,
                            detail={"quantity": remaining, "reason": reason})
                return "stock_ledger", ledger_id, {"ledger_id": ledger_id, "quantity": remaining}

            return self._idempotent(connection, request_id=request_id, action="dispose_batch",
                                    payload=payload, create=create)

    def change_container(self, *, request_id: str, actor_id: str, placement_id: str,
                         new_material: str, reason: str = "") -> Any:
        """更换容器材质；新材质仍须通过该批次 SDS 版本审查，并保留更换轨迹。"""

        payload = {"actor_id": actor_id, "placement_id": placement_id,
                   "new_material": new_material, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, OPERATOR_ROLES)
            placement = self._get_placement(connection, placement_id)
            if placement["status"] == "released":
                raise ValidationError("摆放已经释放")
            batch = self._get_batch(connection, placement["batch_id"])
            self._same_site(actor, self._get_site(connection, batch["site_id"]))
            sheet = self._get_sheet(connection, placement["sheet_id"])
            material_violations = rules.container_violations(new_material, sheet)
            if material_violations:
                raise ChemicalReviewError(material_violations)

            def create():
                now = self._now()
                change_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO container_changes(change_id,placement_id,batch_id,old_material,"
                    "new_material,sheet_id,reason,actor_id,changed_at,request_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (change_id, placement_id, batch["batch_id"],
                     placement["container_material"],
                     new_material, placement["sheet_id"], reason, actor_id, now, request_id),
                )
                connection.execute(
                    "UPDATE chemical_placements SET container_material=? WHERE placement_id=?",
                    (new_material, placement_id),
                )
                self._audit(connection, actor_id=actor_id, action="chemical.container_changed",
                            resource_type="placement", resource_id=placement_id,
                            detail={"batch_id": batch["batch_id"],
                                    "old_material": placement["container_material"],
                                    "new_material": new_material, "sheet_id": placement["sheet_id"]})
                return "container_change", change_id, {"change_id": change_id}

            return self._idempotent(connection, request_id=request_id, action="change_container",
                                    payload=payload, create=create)

    def mark_expired(self, *, actor_id: str, batch_id: str) -> dict[str, Any]:
        """把已过有效期的批次标记过期并隔离其全部在库摆放。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, SAFETY_ROLES)
            batch = self._get_batch(connection, batch_id)
            self._same_site(actor, self._get_site(connection, batch["site_id"]))
            if batch["expiry_date"] >= self._today():
                raise ValidationError("批次尚未超过有效期")
            if batch["status"] == "expired":
                return {"batch_id": batch_id, "status": "expired", "replayed": True}
            connection.execute("UPDATE chemical_batches SET status='expired' WHERE batch_id=?",
                               (batch_id,))
            now = self._now()
            rows = connection.execute(
                "SELECT placement_id FROM chemical_placements WHERE batch_id=? AND status='occupied'",
                (batch_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE chemical_placements SET status='quarantined' WHERE placement_id=?",
                    (row["placement_id"],),
                )
                self._status_event(connection, placement_id=row["placement_id"],
                                   old_status="occupied", new_status="quarantined",
                                   actor_id=actor_id, at=now, reason="expired")
            self._audit(connection, actor_id=actor_id, action="chemical.batch_expired",
                        resource_type="chemical_batch", resource_id=batch_id,
                        detail={"expiry_date": batch["expiry_date"]})
            return {"batch_id": batch_id, "status": "expired"}

    # ------------------------------------------------------------ 泄漏隔离

    def impose_isolation(self, *, request_id: str, actor_id: str, site_id: str, reason: str,
                         unit_id: str | None = None, location_id: str | None = None,
                         batch_id: str | None = None, expires_at: str | None = None) -> Any:
        """实施泄漏隔离：命中范围的在库摆放立即转为隔离，不能再入库或移位。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "reason": reason, "unit_id": unit_id,
                   "location_id": location_id, "batch_id": batch_id, "expires_at": expires_at}
        if not [value for value in (unit_id, location_id, batch_id) if value]:
            raise ValidationError("隔离措施必须指定单元、库位或批次中的至少一个范围")
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, SAFETY_ROLES)
            self._same_site(actor, self._get_site(connection, site_id))
            if not reason.strip():
                raise ValidationError("隔离必须说明原因")
            if unit_id:
                self._get_unit(connection, unit_id, site_id)
            if location_id:
                location = self._get_location(connection, location_id)
                if location["site_id"] != site_id:
                    raise ValidationError("隔离库位不属于该站点")
            if batch_id:
                batch = self._get_batch(connection, batch_id)
                if batch["site_id"] != site_id:
                    raise ValidationError("隔离批次不属于该站点")

            def create():
                now = self._now()
                measure_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO isolation_measures(measure_id,site_id,unit_id,location_id,"
                    "batch_id,reason,started_at,expires_at,lifted_at,imposed_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,NULL,?,?)",
                    (measure_id, site_id, unit_id, location_id, batch_id, reason, now,
                     expires_at, actor_id, now),
                )
                query = ("SELECT p.placement_id, p.status FROM chemical_placements p "
                         "JOIN chemical_batches b ON b.batch_id=p.batch_id WHERE b.site_id=? "
                         "AND p.status='occupied'")
                parameters: list[Any] = [site_id]
                if unit_id:
                    query += " AND p.unit_id=?"
                    parameters.append(unit_id)
                if location_id:
                    query += " AND p.location_id=?"
                    parameters.append(location_id)
                if batch_id:
                    query += " AND p.batch_id=?"
                    parameters.append(batch_id)
                matched = connection.execute(query, parameters).fetchall()
                quarantined = 0
                for candidate in matched:
                    connection.execute(
                        "UPDATE chemical_placements SET status='quarantined' WHERE placement_id=?",
                        (candidate["placement_id"],),
                    )
                    self._status_event(connection, placement_id=candidate["placement_id"],
                                       old_status="occupied", new_status="quarantined",
                                       actor_id=actor_id, at=now, request_id=request_id,
                                       reason=f"isolation:{measure_id}")
                    quarantined += 1
                self._audit(connection, actor_id=actor_id, action="chemical.isolation_imposed",
                            resource_type="isolation_measure", resource_id=measure_id,
                            detail={"reason": reason, "unit_id": unit_id, "location_id": location_id,
                                    "batch_id": batch_id, "expires_at": expires_at,
                                    "quarantined_placements": quarantined})
                return "isolation_measure", measure_id, {"measure_id": measure_id,
                                                         "quarantined_placements": quarantined}

            return self._idempotent(connection, request_id=request_id, action="impose_isolation",
                                    payload=payload, create=create)

    def lift_isolation(self, *, request_id: str, actor_id: str, measure_id: str) -> Any:
        """解除隔离；命中摆放恢复为占用（已过期批次仍保持隔离）。"""

        payload = {"actor_id": actor_id, "measure_id": measure_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, SAFETY_ROLES)
            row = connection.execute("SELECT * FROM isolation_measures WHERE measure_id=?",
                                     (measure_id,)).fetchone()
            if row is None:
                raise NotFoundError("隔离措施不存在")
            measure = dict(row)
            self._same_site(actor, self._get_site(connection, measure["site_id"]))
            if measure["lifted_at"]:
                raise ConflictError("隔离措施已经解除")

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE isolation_measures SET lifted_at=? WHERE measure_id=?", (now, measure_id)
                )
                query = ("SELECT p.placement_id FROM chemical_placements p "
                         "JOIN chemical_batches b ON b.batch_id=p.batch_id WHERE b.site_id=? "
                         "AND b.status='active' AND p.status='quarantined'")
                parameters: list[Any] = [measure["site_id"]]
                if measure["unit_id"]:
                    query += " AND p.unit_id=?"
                    parameters.append(measure["unit_id"])
                if measure["location_id"]:
                    query += " AND p.location_id=?"
                    parameters.append(measure["location_id"])
                if measure["batch_id"]:
                    query += " AND p.batch_id=?"
                    parameters.append(measure["batch_id"])
                restored = 0
                for candidate in connection.execute(query, parameters).fetchall():
                    connection.execute(
                        "UPDATE chemical_placements SET status='occupied' WHERE placement_id=?",
                        (candidate["placement_id"],),
                    )
                    self._status_event(connection, placement_id=candidate["placement_id"],
                                       old_status="quarantined", new_status="occupied",
                                       actor_id=actor_id, at=now, request_id=request_id,
                                       reason=f"lift:{measure_id}")
                    restored += 1
                self._audit(connection, actor_id=actor_id, action="chemical.isolation_lifted",
                            resource_type="isolation_measure", resource_id=measure_id,
                            detail={"restored_placements": restored})
                return "isolation_measure", measure_id, {"measure_id": measure_id,
                                                         "restored_placements": restored}

            return self._idempotent(connection, request_id=request_id, action="lift_isolation",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 台账与盘点

    def _insert_ledger(self, connection, *, batch_id: str, movement_type: str, quantity: float,
                       quantity_unit: str, placement_id: str | None, actor_id: str,
                       request_id: str, at: str, reason: str = "") -> str:
        ledger_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO stock_ledger(ledger_id,batch_id,movement_type,quantity,quantity_unit,"
            "reason,placement_id,actor_id,request_id,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ledger_id, batch_id, movement_type, quantity, quantity_unit, reason, placement_id,
             actor_id, request_id, at),
        )
        return ledger_id

    def _status_event(self, connection, *, placement_id: str, old_status: str | None,
                      new_status: str, actor_id: str, at: str, request_id: str = "",
                      reason: str = "") -> None:
        """记录摆放状态迁移，支撑任意时点的库位状态还原。"""

        connection.execute(
            "INSERT INTO placement_status_events(placement_id,old_status,new_status,reason,"
            "actor_id,request_id,occurred_at) VALUES(?,?,?,?,?,?,?)",
            (placement_id, old_status, new_status, reason, actor_id, request_id, at),
        )

    def ledger_remaining(self, connection, batch_id: str) -> float:
        """按 入库+退库-领用-损耗-销毁 计算台账结存。"""

        remaining = 0.0
        rows = connection.execute(
            "SELECT movement_type,quantity FROM stock_ledger WHERE batch_id=?", (batch_id,)
        ).fetchall()
        for row in rows:
            remaining += MOVEMENT_TYPES[row["movement_type"]] * row["quantity"]
        return round(remaining, 9)

    def _placed_quantity(self, connection, batch_id: str) -> float:
        row = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS total FROM chemical_placements "
            "WHERE batch_id=? AND status IN ('occupied','quarantined')", (batch_id,),
        ).fetchone()
        return round(row["total"], 9)

    def _reduce_placement(self, connection, placement: dict[str, Any], quantity: float,
                          actor_id: str, request_id: str, reason: str = "consumed") -> None:
        remaining = round(placement["quantity"] - quantity, 9)
        now = self._now()
        if remaining <= 1e-9:
            connection.execute(
                "UPDATE chemical_placements SET quantity=0,status='released',released_at=? "
                "WHERE placement_id=?", (now, placement["placement_id"]),
            )
            self._status_event(connection, placement_id=placement["placement_id"],
                               old_status=placement["status"], new_status="released",
                               actor_id=actor_id, at=now, request_id=request_id, reason=reason)
        else:
            connection.execute(
                "UPDATE chemical_placements SET quantity=? WHERE placement_id=?",
                (remaining, placement["placement_id"]),
            )

    def _refresh_batch_status(self, connection, batch_id: str, at: str, actor_id: str) -> None:
        if self.ledger_remaining(connection, batch_id) <= 1e-9:
            connection.execute(
                "UPDATE chemical_batches SET status='depleted' WHERE batch_id=? AND status='active'",
                (batch_id,),
            )

    def count_stock(self, *, request_id: str, actor_id: str, batch_id: str,
                    counted_quantity: float, note: str = "") -> Any:
        """实物盘点：同时比对台账结存与库位实际占用，记录账实差异。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id,
                   "counted_quantity": counted_quantity, "note": note}
        if counted_quantity < 0:
            raise ValidationError("盘点数量不能为负")
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id)
            self._require_role(actor, OPERATOR_ROLES + ("reviewer",))
            batch = self._get_batch(connection, batch_id)
            self._same_site(actor, self._get_site(connection, batch["site_id"]))
            ledger_remaining = self.ledger_remaining(connection, batch_id)
            placed = self._placed_quantity(connection, batch_id)

            def create():
                count_id = uuid.uuid4().hex
                discrepancy = round(counted_quantity - ledger_remaining, 9)
                connection.execute(
                    "INSERT INTO stock_counts(count_id,batch_id,counted_quantity,ledger_remaining,"
                    "placement_quantity,discrepancy,note,counted_by,counted_at,request_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (count_id, batch_id, counted_quantity, ledger_remaining, placed, discrepancy,
                     note, actor_id, self._now(), request_id),
                )
                self._audit(connection, actor_id=actor_id, action="chemical.stock_counted",
                            resource_type="stock_count", resource_id=count_id,
                            detail={"batch_id": batch_id, "counted": counted_quantity,
                                    "ledger_remaining": ledger_remaining,
                                    "placement_quantity": placed, "discrepancy": discrepancy})
                return "stock_count", count_id, {"count_id": count_id, "discrepancy": discrepancy,
                                                 "ledger_remaining": ledger_remaining,
                                                 "placement_quantity": placed}

            return self._idempotent(connection, request_id=request_id, action="count_stock",
                                    payload=payload, create=create)

    def stock_balances(self, site_id: str) -> list[dict[str, Any]]:
        """列出站内所有批次的 入库/领用/损耗/退库/销毁/结存/在库 平衡关系。"""

        with self.database.transaction() as connection:
            batches = connection.execute(
                "SELECT * FROM chemical_batches WHERE site_id=? ORDER BY received_at,batch_id",
                (site_id,),
            ).fetchall()
            result = []
            for row in batches:
                batch = dict(row)
                sums: dict[str, float] = {key: 0.0 for key in MOVEMENT_TYPES}
                for ledger in connection.execute(
                    "SELECT movement_type,SUM(quantity) AS total FROM stock_ledger "
                    "WHERE batch_id=? GROUP BY movement_type", (batch["batch_id"],),
                ):
                    sums[ledger["movement_type"]] = ledger["total"]
                ledger_remaining = self.ledger_remaining(connection, batch["batch_id"])
                placed = self._placed_quantity(connection, batch["batch_id"])
                latest_count = connection.execute(
                    "SELECT counted_quantity,discrepancy,counted_at FROM stock_counts "
                    "WHERE batch_id=? ORDER BY counted_at DESC,count_id DESC LIMIT 1",
                    (batch["batch_id"],),
                ).fetchone()
                result.append({
                    "batch_id": batch["batch_id"], "supplier_name": batch["supplier_name"],
                    "batch_no": batch["batch_no"], "chemical_name": batch["chemical_name"],
                    "status": batch["status"], "quantity_unit": batch["quantity_unit"],
                    "received": round(sums["receive"], 9), "issued": round(sums["issue"], 9),
                    "loss": round(sums["loss"], 9), "returned": round(sums["return"], 9),
                    "disposed": round(sums["disposal"], 9),
                    "ledger_remaining": ledger_remaining, "placed_quantity": placed,
                    "outstanding_with_users": round(ledger_remaining - placed, 9),
                    "last_counted_quantity": latest_count["counted_quantity"] if latest_count else None,
                    "last_count_discrepancy": latest_count["discrepancy"] if latest_count else None,
                    "last_counted_at": latest_count["counted_at"] if latest_count else None,
                    "account_mismatch": bool(latest_count and abs(latest_count["discrepancy"]) > 1e-9),
                })
            return result

    # ------------------------------------------------------------ 追溯与预警

    def _get_batch(self, connection, batch_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM chemical_batches WHERE batch_id=?",
                                 (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return dict(row)

    def _get_placement(self, connection, placement_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM chemical_placements WHERE placement_id=?",
                                 (placement_id,)).fetchone()
        if row is None:
            raise NotFoundError("摆放不存在")
        return dict(row)

    def batch_trace(self, batch_id: str) -> dict[str, Any]:
        """还原某批次从入库起的完整链路：摆放、移位、台账、换容器、SDS 版本。"""

        with self.database.transaction() as connection:
            batch = self._get_batch(connection, batch_id)
            sheet = self._get_sheet(connection, batch["sheet_id"])
            placements = []
            for row in connection.execute(
                "SELECT * FROM chemical_placements WHERE batch_id=? ORDER BY placed_at", (batch_id,)
            ):
                item = dict(row)
                item["sheet_snapshot"] = self._get_sheet(connection, item["sheet_id"])
                rb = connection.execute("SELECT * FROM rule_books WHERE rule_book_id=?",
                                        (item["rule_book_id"],)).fetchone()
                item["rule_book_snapshot"] = (
                    {"rule_book_id": rb["rule_book_id"], "version": rb["version"],
                     "effective_at": rb["effective_at"],
                     "incompatible_pairs": json.loads(rb["incompatible_pairs_json"])}
                    if rb else None)
                placements.append(item)
            ledger = [dict(row) for row in connection.execute(
                "SELECT * FROM stock_ledger WHERE batch_id=? ORDER BY occurred_at,ledger_id",
                (batch_id,))]
            moves = [dict(row) for row in connection.execute(
                "SELECT * FROM placement_moves WHERE batch_id=? ORDER BY moved_at", (batch_id,))]
            changes = [dict(row) for row in connection.execute(
                "SELECT * FROM container_changes WHERE batch_id=? ORDER BY changed_at", (batch_id,))]
            return {"batch": batch, "sheet": sheet, "placements": placements,
                    "ledger": ledger, "moves": moves, "container_changes": changes,
                    "ledger_remaining": self.ledger_remaining(connection, batch_id),
                    "placed_quantity": self._placed_quantity(connection, batch_id)}

    def location_snapshot_at(self, location_id: str, at: str) -> dict[str, Any]:
        """还原任意时点某库位采用的规则配置与实际摆放内容。"""

        with self.database.transaction() as connection:
            location = self._get_location(connection, location_id)
            config = self._location_config_at(connection, location_id, at)
            contents = []
            for row in connection.execute(
                "SELECT * FROM chemical_placements WHERE location_id=? AND placed_at<=? "
                "ORDER BY placed_at",
                (location_id, at),
            ):
                placement = dict(row)
                reductions = connection.execute(
                    "SELECT COALESCE(SUM(CASE WHEN from_placement_id=? THEN quantity ELSE 0 END),0) "
                    "AS moved_out FROM placement_moves WHERE from_placement_id=? AND moved_at<=?",
                    (placement["placement_id"], placement["placement_id"], at),
                ).fetchone()["moved_out"]
                consumed = connection.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS used FROM stock_ledger "
                    "WHERE placement_id=? AND movement_type IN ('issue','loss','disposal') "
                    "AND occurred_at<=?", (placement["placement_id"], at),
                ).fetchone()["used"]
                # 退库会新建摆放，退库量已包含在 initial_quantity 中，此处不再加回。
                quantity_at = round(
                    placement["initial_quantity"] - reductions - consumed, 9)
                if quantity_at <= 1e-9 and placement["released_at"] and placement["released_at"] <= at:
                    continue
                status_events = connection.execute(
                    "SELECT new_status FROM placement_status_events WHERE placement_id=? "
                    "AND occurred_at<=? ORDER BY event_seq DESC LIMIT 1",
                    (placement["placement_id"], at),
                ).fetchone()
                sheet_row = connection.execute("SELECT * FROM safety_sheets WHERE sheet_id=?",
                                               (placement["sheet_id"],)).fetchone()
                rb_row = connection.execute("SELECT * FROM rule_books WHERE rule_book_id=?",
                                            (placement["rule_book_id"],)).fetchone()
                contents.append({
                    "placement_id": placement["placement_id"], "batch_id": placement["batch_id"],
                    "container_material": placement["container_material"],
                    "responsible_actor_id": placement["responsible_actor_id"],
                    "status_at": status_events["new_status"] if status_events else "occupied",
                    "quantity_at": max(quantity_at, 0.0),
                    "sheet_version": sheet_row["version"],
                    "hazard_class": sheet_row["hazard_class"],
                    "concentration": sheet_row["concentration"],
                    "rule_book_version": rb_row["version"] if rb_row else None,
                    "placed_at": placement["placed_at"],
                })
            effective_rule_book = connection.execute(
                "SELECT * FROM rule_books WHERE site_id=? AND effective_at<=? "
                "ORDER BY version DESC LIMIT 1", (location["site_id"], at),
            ).fetchone()
            return {
                "location_id": location_id, "at": at, "config": config,
                "effective_rule_book_version": effective_rule_book["version"]
                if effective_rule_book else None,
                "contents": contents,
            }

    def expiring_certifications(self, site_id: str, before: str) -> list[dict[str, Any]]:
        """提前发现在 before 之前失效且尚未续期的责任人资格。"""

        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT c.* FROM certifications c JOIN actors a ON a.actor_id=c.actor_id "
                "WHERE a.organization_id=(SELECT organization_id FROM sites WHERE site_id=?) "
                "AND c.active=1 AND c.valid_until<=? ORDER BY c.valid_until",
                (site_id, before),
            ).fetchall()
            return [dict(row) for row in rows]

    def isolation_due(self, site_id: str, before: str | None = None) -> list[dict[str, Any]]:
        """发现即将到期（或已过期但未解除）的隔离措施。"""

        before = before or self._today()
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM isolation_measures WHERE site_id=? AND lifted_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at<=? ORDER BY expires_at",
                (site_id, before),
            ).fetchall()
            return [dict(row) for row in rows]

    def expiring_batches(self, site_id: str, before: str) -> list[dict[str, Any]]:
        """发现在 before 之前到期的在库批次。"""

        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT b.* FROM chemical_batches b WHERE b.site_id=? AND b.status='active' "
                "AND b.expiry_date<=? ORDER BY b.expiry_date", (site_id, before),
            ).fetchall()
            return [dict(row) for row in rows]
