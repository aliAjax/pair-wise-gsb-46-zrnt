# 空地转运台（急救车调度 + 直升机分流）

纯 Python 标准库实现的应急医疗转运原型，使用 SQLite 持久化，HTTP 接口由 `http.server` 提供。
覆盖地面急救车调度，以及山区公路中断时的直升机空地转运：资源登记、派单核对、缺口留痕、起飞锁定、改降释放床位与伤员全程时间线。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：地面急救车状态转换、优先级评分与能力匹配。
- `src/air_rules.py`：空地转运规则——登记校验、天气/航程/载重/机位/余量核对、状态转换。
- `src/repository.py`：SQLite 建表、乐观锁事务、多实体原子批处理与事件查询。
- `src/service.py`：地面急救车用例编排。
- `src/air_service.py`：空地转运用例编排——派单、起飞、改降、交接、取消。
- `src/http_api.py`：HTTP 路由与统一错误响应。
- `src/audit.py`：地面流程审计事件。
- `static/index.html`：最小演示页面（含一键端到端演示）。
- `tests/`：完整流程、规则计算、失败场景与空地转运测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8322
```

默认端口为 `8322`，服务启动时自动建表（旧库自动补齐空地转运新表）。

## 空地转运业务说明

**资源登记**

- 伤员：伤情等级（critical/urgent/stable）、体重 kg、是否需氧与需氧量 L/min、接运位置。初始进入候派区 `waiting`。
- 直升机：基地、续航 km、载重上限 kg、机位数、夜航资质。状态：`available / reserved / airborne`。
- 医院：名称、机位总量、是否具备氧疗收治条件。余量 = 总量 − 已预留床位（已派单/空中）− 已交接床位。

**派单核对**（`POST /api/air/patients/{id}/dispatch`）

按顺序核对，任一不满足则伤员留在候派区，返回缺口代码与核算指标，并把本次核对写入伤员时间线与 `last_gap`：

- 天气需电话确认（`weather_phone_confirmed`）且确认可飞（`weather_ok`）；
- 夜间任务要求直升机具备夜航资质；
- 去程 + 回程里程不得超过续航的 90%（留 10% 余量）；
- 机组 + 设备 + 本架机已锁定伤员 + 本次伤员的总重不得超过载重上限；
- 直升机机位未满；需氧伤员要求目标医院支持氧疗；
- 医院余量需电话确认（`beds_phone_confirmed`）且有余量。

核对通过后伤员 `assigned`，直升机进入 `reserved`，目标医院床位预留（乐观锁版本递增，防止超额派单）。

**起飞与锁定**：`takeoff` 后伤员 `airborne`，直升机 `airborne` 整机锁定，不再接受派单；同机已派单伤员随机一并空中。

**改降**：`divert` 在同一事务内先释放原医院预留床位，再校验并锁定新医院床位；新医院必须在诊且满足氧疗条件，不能与原医院相同。

**交接**：`handover` 后伤员 `handed_over`，预留床位转为实际占用；机上无其他在途伤员时直升机回到 `available`。

**取消**：`cancel` 释放所占机位与床位并恢复直升机状态。

**时间线**：沿伤员可看到登记、每次派单核对（含失败缺口与指标）、派单、起飞、改降、交接的完整事件链；直升机与医院侧也各自记录机位/床位的锁定与释放事件。

## 主要接口

地面（原有）：

- `GET /health`、`GET /`、`GET /api/records`、`GET /api/records/{id}`、`GET /api/records/{id}/audit`、`GET /api/stats`
- `POST /api/records`、`POST /api/records/{id}/actions/{action}`

空地转运：

- `POST /api/air/patients` / `POST /api/air/helicopters` / `POST /api/air/hospitals`：登记，请求体 `{"code":"...","data":{...}}`。
- `GET /api/air/patients?state=waiting`、`GET /api/air/helicopters`、`GET /api/air/hospitals`：列表（直升机/医院附带已占与剩余机位）。
- `GET /api/air/patients/{id}`、`GET /api/air/helicopters/{id}`、`GET /api/air/hospitals/{id}`：详情。
- `POST /api/air/patients/{id}/dispatch`：派单核对，见业务说明。
- `POST /api/air/patients/{id}/actions/takeoff|handover|cancel`：状态动作，请求体含 `expected_version`。
- `POST /api/air/patients/{id}/divert`：改降，`{"expected_version":N,"data":{"target_hospital_id":M,"note":"..."}}`。
- `GET /api/air/patients/{id}/timeline`：伤员事件时间线。
- `GET /api/air/stats`：三类资源状态计数。

除 `/health` 和 `/` 外，请求需提供 `X-User-Id`、`X-Role`，可选 `X-Org`。
角色：`dispatcher`（调度派单/登记）、`pilot`（起飞/改降）、`paramedic`（改降/交接/取消）、`hospital_coordinator`（登记医院/交接）、`logistics`（登记资源）、`admin`（全部）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖地面完整流程与规则、权限拒绝和版本冲突，以及空地转运的全流程（派单→起飞→改降→交接）、各类派单缺口、取消释放、状态守卫与权限。
