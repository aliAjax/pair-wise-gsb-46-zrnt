# 急救车调度与空地转运分流

纯Python标准库实现的急救调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。在原有地面急救车调度之上，扩展了**空地转运台**：山区公路中断时，用直升机分流伤员，覆盖登记、派单核对、候派缺口、起飞锁定、改降与伤员时间线。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：地面调度的状态转换、优先级评分、能力匹配、车辆冲突检查。
- `src/airrules.py`：空地转运纯规则——登记校验、天气/里程/载重/医院核对、缺口计算。
- `src/repository.py`：SQLite建表、事务（含跨表锁定的`AirTransaction`）和查询。
- `src/service.py`：地面调度用例编排、权限检查、乐观并发和审计。
- `src/airservice.py`：空地转运用例编排——登记、派单、重核、起飞、改降、交接、时间线。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：地面调度事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：地面流程、空地流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8322
```

默认端口为`8322`，默认数据库位于项目目录。服务启动时自动建表。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色：`admin`、`dispatcher`、`paramedic`、`pilot`、`hospital_coordinator`。

## 空地转运业务规则

**登记**

- 伤员：伤情等级（critical/urgent/stable）、伤情描述、体重kg、需氧量L/min、接救点。
- 直升机：编号、基地、机位、续航km、可用载重kg、夜间资质、机载氧源。
- 医院：名称、床位总数、直升机机位总数、夜间接收、氧源、收治能力（BLS/ALS）。

**派单核对**（创建任务或`recheck`时执行，电话确认项由调度员在数据中显式给出）

- 天气：`weather_confirmed`且`weather_flyable`，附电话通报摘要。
- 夜间：夜航时直升机需夜间资质、医院需夜间接收。
- 往返里程：基地→接救点→医院→基地 + 20km备份 ≤ 续航。
- 载重：伤员体重 + 氧源钢具12kg（需氧>0时）+ 机组设备200kg ≤ 可用载重。
- 医院：电话确认收治条件，且床位余量>0、机位余量>0、能力与氧源满足伤情。
- 直升机已被其他任务锁定（起飞后）则不可再派。

核对不通过：任务留在`waiting_dispatch`候派区，`payload.last_gaps`写明每条结构化缺口（code+message，含数值缺口如`缺口26.0kg`）；通过后进入`assigned`。

**状态机**：`waiting_dispatch/assigned →(takeoff)→ airborne →(divert)→ airborne →(handover)→ closed`；候派/已派单可`cancel`；`recheck`可反复重核，结果直接决定留在候派区还是可起飞。

**起飞锁定**：`takeoff`前用最新余量再核一遍，通过后同一事务内锁定直升机（`status=locked`、记录任务与机位）并在医院预留床位+机位各1。

**改降**：`divert`先核对新医院（收治条件、余量、剩余续航能否完成新航段），不通过则拒绝且原锁定不变；通过后**先释放原医院床位/机位，再锁定新医院**，全程一个事务。

**交接**：`handover`后预留床位转占用、机位释放、直升机解锁恢复可用，伤员状态变为`delivered`。

**时间线**：`GET /api/patients/{id}/timeline`沿伤员看到登记、每次派单评估、起飞、改降去向和交接；`GET /api/missions/{id}/timeline`为任务视角。

## 主要接口

地面调度（原有）：

- `GET /health`、`GET /`、`GET /api/records`、`GET /api/records/{id}`、`GET /api/records/{id}/audit`、`GET /api/stats`
- `POST /api/records`、`POST /api/records/{id}/actions/{action}`

空地转运：

- `POST /api/patients`：`{"reference":"P-001","data":{...}}`登记伤员；`GET /api/patients`、`GET /api/patients/{id}`、`GET /api/patients/{id}/timeline`
- `POST /api/aircraft`：`{"data":{...}}`登记直升机；`GET /api/aircraft`、`GET /api/aircraft/{id}`
- `POST /api/hospitals`：`{"data":{...}}`登记医院；`GET /api/hospitals`、`GET /api/hospitals/{id}`
- `POST /api/missions`：`{"reference":"M-001","data":{"patient_id":1,"aircraft_id":1,"hospital_id":1,"weather_confirmed":true,"weather_flyable":true,"hospital_confirmed":true,"night_flight":false,"base_to_scene_km":68,"scene_to_hospital_km":45,"hospital_to_base_km":22}}`
- `POST /api/air/evaluate`：只读试算派单核对，不落库
- `GET /api/missions?state=waiting_dispatch`：候派区列表；`GET /api/missions/{id}`、`GET /api/missions/{id}/timeline`
- `POST /api/missions/{id}/actions/{recheck|takeoff|divert|handover|cancel}`：`{"expected_version":2,"data":{...}}`
- `GET /api/airstats`：伤员/任务/直升机/医院余量统计

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖地面与空地完整流程、规则计算、候派缺口、改降释放、权限拒绝、重复引用和版本冲突。
