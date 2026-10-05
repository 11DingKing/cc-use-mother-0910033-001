# 跨场馆志愿者借调

本项目维护跨场馆志愿者借调的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖文博中心运营员、志愿者、监护人、场馆负责人，并明确跨馆资格约束、双边确认流程、排班占用迁移、关联方权限隔离等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/secondment/`：借调后端（领域模型、候选评估、双边确认、原子迁移、可见性、HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/serve.py`：启动后端服务。
- `tests/`：契约完整性与后端行为回归测试。

## 后端设计

契约四条不变量与代码的对应关系：

- **跨馆资格约束**：`matching.py` 统一评估技能等级、监护限制、原排班、接收方容量，候选附打分拆解（排序解释），被拒者附冲突原因；同一套检查在确认迁移时再次强制执行。
- **双边确认流程**：`service.confirm` 收集原场馆、接收方（必要时监护人）的确认，协议日期取双方确认日期的交集（部分日期接受）。
- **排班占用迁移**：双方确认齐备后，在同一把锁内先整体校验再落变更——释放原排班、建立占用、扣减容量，全成功或全失败；`recall` 按日期回收占用并恢复排班。
- **关联方权限隔离**：`visibility.py` 界定借调链（原场馆 + 进行中借调的接收方），敏感资料仅运营员、本人、监护人与链上场馆可见，归档后接收方自动退出借调链。

一致性约定：重复邀请与重复确认幂等；并发确认由锁串行化，后到者得到 `date_occupied` / `capacity_full` 等冲突原因，容量或占用释放后重新确认即可重试；`expected_version` 提供乐观锁。

## 运行

启动服务（`--seed` 注入节假日借调演示数据）：

```bash
python3 tools/serve.py --port 8000 --seed
```

调用者身份通过请求头传递：`X-Actor-Role`（operator / volunteer / guardian / venue_manager）、`X-Actor-Id`、`X-Venue-Id`、`X-Volunteer-Id`。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/venues`、`/venues/{id}/capacity` | 登记场馆与接收方容量 |
| POST | `/volunteers`、`/volunteers/{id}/shifts` | 登记志愿者（技能、敏感资料、监护限制）与原排班 |
| GET | `/volunteers/{id}`、`/volunteers/{id}/calendar` | 脱敏视图 / 链内排班占用日历 |
| POST | `/loan-requests` | 登记借调请求（技能需求、日期、差旅、授权范围） |
| POST | `/loan-requests/{id}/proposals` | 生成候选（排序解释 + 冲突原因） |
| POST | `/loan-requests/{id}/invitations` | 发出邀请（重复邀请幂等） |
| POST | `/invitations/{id}/confirm`、`/decline` | 双边确认（支持部分日期）/ 拒绝 |
| POST | `/agreements/{id}/recall`、`/advance` | 临时召回 / 推进执行与归档 |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
