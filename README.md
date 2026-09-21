# 森林碳益共享账

面向集体林权改革与林下产业协作的碳收益分配账本。基线/方法、监测证据、
贡献份额由不同机构维护，系统用**事件溯源 + 每期版本锁定**保证：监测更正、
共有林地份额变化、项目边界变化、重复核证回执、负调整与争议冻结都只能以
新事件影响余额，已支付款项永不被静默改写。

## 快速开始

```bash
# 运行全部测试（41 个）
python3 -m unittest discover -s tests -v

# 首期结算端到端演示（先付部分 → 监测更正 → 追补 → 份额/边界变化 → 冻结）
python3 -m service.demo

# 启动 HTTP 服务
python3 -m service.main          # 默认 http://0.0.0.0:3000，事件写入 ledger-events.jsonl
curl http://127.0.0.1:3000/health
```

删除 `demo-ledger.jsonl` / `LEDGER_PATH` 指定的文件可重置账本。

## 核心能力

* **每期锁定**：确认时把方法版本、监测证据（含核证回执）、碳价、边界、
  份额规则、可分配量、各方金额拍成快照，计算 `allocation_hash`。
* **跨期更正不重写历史**：证据更正后旧版本转为 `stale`，新版本生成
  `true_up` 追补流水（可正可负）；STALE 期间禁止支付。
* **先付后更正的资金闭环**：已付款大于新应分形成追补收回责任，参与方
  退回（claimback）；STALE 期收回的暂缓款在新版本确认时一次结清。
* **共有份额 / 边界变化**：新生效规则只影响之后确认的版本。
* **重复核证回执拒绝**：一份回执只能占用一份证据。
* **争议冻结**：分对外支付 / 对内收回两个方向，冻结额不可操作。
* **分角色可见**：林农/运营方只见本方账单且敏感位置脱敏，审计不见
  证件号，监测不见财务，公众只能凭哈希存证核验；各方下载到同一哈希。
* **防篡改**：事件链式哈希 + JSONL 持久化，重放结果一致，篡改可检出。

## HTTP 接口（摘要）

身份用请求头模拟：`X-Role`（admin/auditor/monitor/operator/
forest_farmer/public）、`X-Party-Id`（参与方绑定）。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/methods` `/api/parties` `/api/plots` `/api/boundaries` `/api/shares` `/api/periods` | admin | 主数据 |
| POST | `/api/receipts` | admin/monitor | 核证回执登记 |
| POST | `/api/evidence` | admin/monitor | 提交/更正监测证据 |
| POST | `/api/periods/{p}/confirm` | admin | 确认并锁定分配版本 |
| POST | `/api/periods/{p}/withhold` `/pay` `/release-withheld` `/claim-back` | admin | 暂缓/支付/解除/收回 |
| POST | `/api/disputes` `/api/disputes/{id}/resolve` | admin | 争议冻结/解除 |
| GET | `/api/periods/{p}/summary` | admin/auditor | 全方余额汇总 |
| GET | `/api/periods/{p}/versions` | admin/auditor | 历史锁定版本 |
| GET | `/api/periods/{p}/statements/{party}` | 本方或 admin/auditor | 可下载对账依据 |
| GET | `/api/events` | admin/auditor | 事件链审计（审计隐去证件号） |
| GET | `/api/anchor/{hash}` | 任意（含公众） | 存证核验 |

所有写接口支持在 JSON 体里传 `idem_key` 实现幂等；业务规则冲突返回 409，
越权返回 403。金额一律以**分**为整数，碳量以**千克 CO₂e**为整数。

## 示例

```bash
curl -s -X POST http://127.0.0.1:3000/api/periods/2026H1/confirm \
  -H 'X-Role: admin' -H 'Content-Type: application/json' -d '{}'
curl -s http://127.0.0.1:3000/api/periods/2026H1/statements/F1 \
  -H 'X-Role: forest_farmer' -H 'X-Party-Id: F1'
```

详见 [docs/domain.md](docs/domain.md) 与 `service/demo.py`。
