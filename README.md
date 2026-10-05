# 危险化学品入站与共储审查服务

本项目在极地科考站协作基础服务之上，提供化学品入站、共储兼容审查、流转台账与全程追溯能力，
用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与
哈希串联审计。

## 解决的问题

同一供应商名称下不同批次的固定液/清洗剂可能采用不同危险分类。系统在每次入库或移位时，
基于**批次实际版本的安全数据**逐项审查，杜绝凭人工标签摆放导致禁忌物进入同一防泄漏单元：

- **版本化安全数据**：SDS 按 (供应商, 批次, 版本) 登记实际浓度、所需温区、容器材质、
  危险分类、不相容分类与所需屏障等级；规则册按站点发布版本，只追加、不覆盖。
- **一次摆放冻结一组快照**：每条摆放记录都保存当时的 SDS 版本（sheet_id）、规则册版本
  （rule_book_id）和库位配置版本；规则更新**不回写**已发生的摆放事实。
- **审查即事务**：温区覆盖、容器材质、屏障等级、责任人资格、规则册禁忌对、SDS 自声明
  不相容、库位容量、有效隔离措施全部在单个 `BEGIN IMMEDIATE` 事务内检查；审查失败整体回滚，
  不留下任何批次、占用或台账。
- **并发只许一个成功者**：写事务立即获取库级写锁，争抢同一剩余容量时恰好一个成功。
- **重试不重复增减**：所有写操作以 request_id 幂等，重复请求返回首次回执，数量不变。
- **全程追原批次**：泄漏隔离、部分领用、退库、过期、销毁、更换容器都以 batch_id 串联；
  退库与移位产生的新摆放仍引用原批次的 SDS 版本。
- **任意时点还原**：可查询某库位在指定时点的配置版本、生效规则册版本与实际摆放内容
  （含隔离状态与当时数量）。
- **提前预警**：即将失效的责任人资格、隔离措施与即将过期的批次。
- **账实平衡**：按 入库 + 退库 − 领用 − 损耗 − 销毁 计算台账结存，盘点记录与台账、
  库位实际占用的差异，`stock-balances` 直接输出平衡关系。

## 目录

- src/polar_station_foundation/domain.py、models.py、service.py、audit.py、clock.py、
  storage.py、errors.py：基础主体、权限、幂等、审计与存储；
- src/polar_station_foundation/chemical.py：共储兼容纯规则（温区/材质/屏障/禁忌/资格）；
- src/polar_station_foundation/chemical_service.py：化学品主数据、审查入库、移位、领用、
  退库、损耗、销毁、隔离、过期、盘点、追溯与预警事务服务；
- src/polar_station_foundation/api.py：HTTP/JSON 边界（含 /chemical/* 路由）；
- src/polar_station_foundation/acceptance.py：离线端到端验收；
- tests/：规则、事务边界、幂等并发语义、版本快照、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m polar_station_foundation.acceptance

验收会在临时 SQLite 数据库中完成主体建档、规则册/库位/SDS 登记、双批次入库（同供应商不同
危险分类）、禁忌试评拒绝、部分领用、盘点差异、规则册升版不回写、时点快照、泄漏隔离与解除，
成功时输出 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

写入接口通过 X-Actor-Id 标识操作者（请求体中的 actor_id 被忽略，以头部为准）。

### 化学品接口（均为 POST，除标注 GET 外）

| 路径 | 说明 |
| --- | --- |
| /chemical/rule-books | 发布规则册版本（acid/base 等禁忌对），安全官 |
| /chemical/units | 登记防泄漏单元及屏障等级 |
| /chemical/locations | 登记库位（容量、温区、屏障、所需资格、所属单元） |
| /chemical/locations/update | 库位配置升版（历史版本保留） |
| /chemical/safety-sheets | 登记某供应商某批次某版本 SDS |
| /chemical/certifications | 授予责任人资格及有效期 |
| /chemical/propose | 只读试评摆放，不产生占用 |
| /chemical/inbound | 审查通过后入库：建批次+锁库位+台账 |
| /chemical/relocate | 整体或部分移位（目标库位重新审查） |
| /chemical/issue | 部分领用；余量为零自动释放占用 |
| /chemical/loss | 损耗登记（必须填原因） |
| /chemical/returns | 退库（追原批次，重新审查） |
| /chemical/disposal | 销毁批次全部余量并释放占用，安全官 |
| /chemical/container-change | 更换容器材质（须通过该批次 SDS 版本校验） |
| /chemical/isolation/impose | 实施泄漏隔离（单元/库位/批次范围，可设到期时间） |
| /chemical/isolation/lift | 解除隔离 |
| /chemical/expired | 标记过期批次并隔离其摆放，安全官 |
| /chemical/stock-counts | 实物盘点，记录账实差异 |
| GET /chemical/stock-balances?site_id= | 入库/领用/损耗/退库/销毁/结存/在库平衡 |
| GET /chemical/batches/{id}/trace | 批次全链路追溯（摆放、移位、台账、换容器、快照） |
| GET /chemical/locations/{id}/snapshot?at= | 任意时点库位规则与实际内容 |
| GET /chemical/alerts?site_id=&cert_before=&iso_before=&batch_before= | 到期预警 |

审查不通过返回 422，响应体 `violations` 为逐条原因列表；重试同一 request_id 返回 200 与
首次回执，首次成功返回 201。
