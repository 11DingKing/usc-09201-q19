# 森林碳益共享账领域约定

项目面向集体林权改革与林下产业协作场景，业务记录需要区分来源、发生时间与当前有效版本。涉及个人、机构或敏感地点的信息只应向职责范围内的角色开放，所有对外结果都应能够追溯到采用的数据版本。

## 1. 核心原则：只追加，不重算

账本是一条带哈希链的**只追加事件流**。以下情形都只能通过“新事件”改变当前余额，任何历史事件及其哈希永不就地修改：

| 情形 | 处理方式 |
| --- | --- |
| 监测结果跨期更正 | `monitoring.corrected` 新版本，确认仍锁定旧监测 |
| 共有林地份额变化 | 新结算期上新的 `share.rule.locked` 版本；已确认期禁止再加份额版本 |
| 项目边界变化 | `boundary.changed` 新版本，仅被以后各期的监测/确认引用 |
| 重复核证回执 / 网络重试 | `event_id` 幂等返回既有事件；相同业务编号另提则拒绝 |
| 负调整 | `negative.adjustment` 独立事件，必须有原因 |
| 争议冻结 | `dispute.frozen` / `dispute.resolved` 成对的新事件 |

这样，已经支付的款项始终可以用当时的确认事件、监测事件、方法/基线/边界版本、份额规则版本逐条解释，不会因日后更正而“无从解释”。

## 2. 每期确认即锁定

`distribution.confirmed`（确认）必须同时锁定并校验：

- 方法版本 `method_version`（方法学机构维护）
- 基线版本 `baseline_version`（基线机构维护）
- 边界版本 `boundary_version`（边界机构维护，边界变化即新版本）
- 份额规则版本 `share_rule.version`（共有林地各参与方份额，之和严格为 1）
- 当期最新监测事件 `monitoring_ref` 及其证据清单（`ref` + `sha256`）
- 核证回执（缺回执不能确认）
- 可分配量与逐行金额：各行份额必须等于锁定份额，金额合计严格等于可分配量；分位尾差按最大余数法整分，**不允许四舍五入产生一分钱差异**

确认后即生成“确认流水”，其事件哈希成为该期所有依据包的共同锚点。

## 3. 账户口径（人民币分，整数运算）

对某期某参与方：

- **应得（gross）**：确认时按锁定份额分到的金额，永不改变
- **权益（entitlement）** = 应得 + 历次追补差额 + 历次负调整
- **净支付（paid_net）** = 支付（pay）累计 − 返还（return）累计
- **暂缓（holds_active）**：生效中的暂缓金额，暂缓不是出账
- **剩余应付（remaining_payable）** = max(权益 − 净支付 − 暂缓, 0)
- **追补责任（clawback_due）** = max(净支付 − 权益, 0)，即实际超付、应返还的部分

注意追补责任**不**把暂缓计入：暂缓的钱尚未离开账，更正后只需解除多余暂缓，不会虚增返还义务。

### 追补流水（trueup）

监测更正后，系统以**确认时锁定的份额**对新可分配量重新整分，给出每方有符号建议差额。办公室逐方发起 `trueup.raised`，流水中拆成两部分：

- `payable_offset`：负差额先冲减该方剩余应付；
- `clawback`：负差额超过“尚未支付部分”的金额，形成追补责任，只能通过 `direction=return` 的支付流水返还，且返还额不得超过当前责任额。

正差额则增加剩余应付，走正常支付流水补付。

## 4. 四类流水

每期可导出：**确认（confirmed）、暂缓（holds，含解除记录 hold_release）、支付（payments）、追补（trueups）**，另附负调整与争议事件清单。每条流水都带事件序号、事件哈希与发生时间。

争议冻结期间只阻止**资金动作**（支付 pay / 返还 return）；追补、负调整、暂缓解除属于账簿更正，仍可正常入账，避免争议期账簿无法更正。

## 5. 分角色可见，同一本账

所有角色（办公室、各机构、林农/运营方）拿到的账本哈希根 `ledger_root_hash` 与各事件哈希完全一致；差异只在字段裁剪：

- 银行账号、证件号、联系方式、支付凭证号等敏感交易字段：仅办公室全量可见；机构视角打码；
- 监测证据明细（文件 ref）：仅监测机构可见，其他机构只见 `sha256`；
- 参与方依据包（evidence-pack）只含本方金额与本方流水，但锁定版本、监测/核证哈希、确认哈希与账本根与各方一致——林农与运营方可以独立核对“我分到的钱”和“大家共同认定的依据”。

## 6. 事件类型与提交角色

| 事件 | 允许提交角色 |
| --- | --- |
| `method.registered` | methodology_body |
| `baseline.registered` | baseline_body |
| `boundary.changed` | boundary_body |
| `participant.registered` / `period.opened` / `share.rule.locked` / `distribution.confirmed` / `hold.*` / `dispute.*` / `payment.made` / `negative.adjustment` / `trueup.raised` | office |
| `monitoring.submitted` / `monitoring.corrected` | monitoring_body |
| `verification.receipt.recorded` | verification_body |

角色鉴权先于业务校验；校验失败不会写入任何事件。
