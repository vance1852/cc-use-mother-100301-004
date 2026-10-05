"""化学品共储兼容性的纯规则评估。

所有函数只依据调用方传入的版本化快照（SDS 版本、规则册版本、库位配置版本）
作出判断，不读取数据库，因此规则更新不会影响已经落库的摆放事实：历史摆放
保存其当时所用的快照，重新评估时只需重新取出快照调用同一组函数。
"""

from __future__ import annotations

from typing import Any, Iterable


def normalize_pairs(pairs: Iterable[Iterable[str]]) -> set[frozenset[str]]:
    """把规则册的无序禁忌对转换为集合。"""

    normalized: set[frozenset[str]] = set()
    for pair in pairs:
        values = [str(item).strip() for item in pair]
        if len(values) != 2 or not all(values):
            raise ValueError("禁忌规则对必须恰好包含两个非空危险分类")
        normalized.add(frozenset(values))
    return normalized


def temperature_violations(sheet: dict[str, Any], location: dict[str, Any]) -> list[str]:
    """检查批次所需温区是否被库位温区完整覆盖。"""

    violations: list[str] = []
    if location["temp_min"] > sheet["temp_min"] + 1e-9:
        violations.append(f"库位温区下限 {location['temp_min']} 高于批次要求 {sheet['temp_min']}")
    if location["temp_max"] < sheet["temp_max"] - 1e-9:
        violations.append(f"库位温区上限 {location['temp_max']} 低于批次要求 {sheet['temp_max']}")
    return violations


def container_violations(container_material: str, sheet: dict[str, Any]) -> list[str]:
    """检查容器材质是否在该 SDS 版本允许的范围内。"""

    allowed = {str(item).strip() for item in sheet["container_materials"]}
    if container_material not in allowed:
        return [f"容器材质 {container_material} 不在 SDS v{sheet['version']} 允许列表 {sorted(allowed)} 中"]
    return []


def barrier_violations(sheet: dict[str, Any], location: dict[str, Any],
                       unit: dict[str, Any]) -> list[str]:
    """检查库位与防泄漏单元的屏障等级是否满足 SDS 要求。"""

    required = sheet["required_barrier_level"]
    violations: list[str] = []
    if location["barrier_level"] < required:
        violations.append(f"库位屏障等级 {location['barrier_level']} 低于要求 {required}")
    if unit["barrier_level"] < required:
        violations.append(f"防泄漏单元屏障等级 {unit['barrier_level']} 低于要求 {required}")
    return violations


def coexistence_violations(sheet: dict[str, Any],
                           occupants: Iterable[dict[str, Any]],
                           rule_pairs: set[frozenset[str]]) -> list[str]:
    """检查同一防泄漏单元内既有批次是否与待入批次互为禁忌物。

    occupants 中的每条记录至少包含 batch_id 与 hazard_class；同一批次的不同
    摆放（例如部分领用后的余量）永远视为相容。
    """

    candidate_class = sheet["hazard_class"]
    own_incompatible = {str(item).strip() for item in sheet.get("incompatible_classes", [])}
    violations: list[str] = []
    for occupant in occupants:
        if occupant["batch_id"] == sheet["batch_id"]:
            continue
        other_class = occupant["hazard_class"]
        other_incompatible = {str(item).strip() for item in occupant.get("incompatible_classes", [])}
        reasons: list[str] = []
        if frozenset((candidate_class, other_class)) in rule_pairs:
            reasons.append("规则册禁忌对")
        if other_class in own_incompatible:
            reasons.append(f"SDS v{sheet['version']} 声明不相容分类")
        if candidate_class in other_incompatible:
            reasons.append("同单元既有批次 SDS 声明不相容分类")
        if reasons:
            violations.append(
                f"与同单元批次 {occupant['batch_id']}（{other_class}）冲突：{'、'.join(reasons)}"
            )
    return violations


def certification_violations(required: Iterable[str], held: Iterable[dict[str, Any]],
                             at: str) -> list[str]:
    """检查责任人在指定时点是否持有所需的全部有效资格。"""

    held_by_type: dict[str, dict[str, Any]] = {}
    for cert in held:
        if cert["active"] and cert["valid_from"] <= at <= cert["valid_until"]:
            held_by_type[cert["certification_type"]] = cert
    violations: list[str] = []
    for required_type in required:
        if required_type not in held_by_type:
            violations.append(f"缺少在 {at} 有效的资格：{required_type}")
    return violations
