# 跨场馆志愿者借调

本项目维护跨场馆志愿者借调的领域约定、角色边界与样例数据，并提供可运行的 Python 后端（纯标准库，零第三方依赖），解决节假日客流突增时多场馆同时借调熟练志愿者的协调问题：原场馆既有排班可见、差旅占用（借调排班）统一迁移、候选排序与冲突原因可解释、敏感资料按借调链隔离。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/secondment/`：借调后端（领域模型、业务服务、HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/demo_scenario.py`：节假日突增场景端到端演示。
- `tests/`：契约回归、服务流程与并发、HTTP 接口测试。

## 后端设计（与契约不变量对应）

| 契约不变量 | 实现 |
| --- | --- |
| 跨馆资格约束 | 候选评估强制校验技能等级、监护限制（窗口/时长/是否允许跨馆）、逐日期占用冲突 |
| 双边确认流程 | 原场馆应答（可仅接受部分日期）→ 接收方确认才生效；顺序由状态机强制 |
| 排班占用迁移 | 确认时在单一锁临界区内完成冲突复核、容量扣减、原排班释放与借调占用迁入，整体原子；召回逆向恢复 |
| 关联方权限隔离 | 敏感资料仅志愿者本人、监护人、原场馆、借调链接收方可见，其余调用方得到脱敏视图 |

一致性保证：

- **部分日期接受**：原场馆可只接受提议日期的子集，协议仅覆盖被接受日期。
- **临时召回**：借调双方均可按日期子集召回，占用原子迁回原场馆，容量即时释放。
- **重复邀请**：同一请求对同一志愿者仅存在一条有效邀请，重复提交幂等返回原邀请；重复确认/重复应答同样幂等。
- **并发确认**：多请求争抢同一志愿者或同一容量时，临界区串行化校验，仅一个成功，失败方收到 `ALREADY_LOANED` / `CAPACITY_FULL` 及冲突明细。
- **可解释**：候选报告给出逐项打分解释（日期覆盖 / 技能富余 / 迁移惩罚）与被拒原因代码；请求详情附带审计事件流。

## HTTP 接口

启动：`PYTHONPATH=src python3 -m secondment --port 8000`

身份通过请求头传递：`X-Actor-Id`、`X-Actor-Role`（`operator`/`volunteer`/`guardian`/`venue_manager`）、`X-Actor-Venue-Id`（场馆工作人员必填）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/venues` `/volunteers` `/guardian-restrictions` `/schedules` `/capacities` | 登记场馆、志愿者、监护限制、原排班、接收方容量 |
| POST | `/loan-requests` | 登记借调请求（技能需求 + 日期 + 每日时长） |
| GET | `/loan-requests/{id}` | 请求详情（派生状态、需求满足度、审计事件） |
| GET | `/loan-requests/{id}/proposals` | 候选排序与被拒名单（均附原因） |
| POST | `/loan-requests/{id}/invitations` | 发出邀请（重复邀请幂等返回 200） |
| POST | `/invitations/{id}/respond` | 原场馆应答（`accepted_dates` 支持部分接受） |
| POST | `/invitations/{id}/confirm` | 接收方确认，原子迁移占用 |
| POST | `/agreements/{id}/recall` | 临时召回（可指定日期子集） |
| GET | `/volunteers/{id}` `/volunteers/{id}/schedule` | 志愿者视图 / 排班去向（按借调链过滤） |

错误统一返回 `{"error": {"code", "message", "details"}}`，冲突代码包括 `SKILL_MISMATCH`、`GUARDIAN_*`、`ALREADY_LOANED`、`CAPACITY_FULL`、`SCHEDULE_CONFLICT`、`NOT_CANDIDATE` 等。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

场景演示：`python3 tools/demo_scenario.py`
