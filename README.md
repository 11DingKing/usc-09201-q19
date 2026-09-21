# 森林碳益共享账

面向集体林权改革与林下产业协作的碳收益共享账本：**事件溯源、只追加、每期锁定版本与证据、分角色可见**。基线、监测周期与贡献比例由不同机构分别维护，跨期更正、共有林地份额变化、重复核证回执、负调整与争议冻结全部通过新事件影响余额，历史确认与已付款项永不重算。

## 能力一览

- 每期确认锁定：方法版本、基线版本、边界版本、共有林地份额规则、监测证据（ref+sha256）、核证回执、可分配量与逐行分配；
- 只追加哈希链：每条事件含前序哈希，文件被篡改会在加载时报错；`event_id` 幂等，重复提交/重复回执不产生第二条流水；
- 完整账户口径：剩余应付、暂缓、追补责任（超付返还义务）逐期逐方可查；
- 四类流水：确认、暂缓、支付、追补（附负调整与争议事件），均带事件哈希；
- 分角色下载依据：所有人共享同一哈希根，银行账号/凭证号等敏感字段按角色裁剪，林农依据包只含本方金额；
- 金额按人民币分整数运算，分位尾差整分，合计严格等于可分配量。

## 运行

```bash
python3 -m unittest                       # 32 个测试
python3 scripts/demo_settlement.py        # 首期结算完整故事线演示
PORT=3000 python3 -m service.main         # HTTP 服务
LEDGER_FILE=data/ledger.jsonl PORT=3000 python3 -m service.main  # 带持久化
```

## HTTP 接口

演示用请求头表达身份：`X-Role`（office / monitoring_body / baseline_body / methodology_body / boundary_body / verification_body / participant），参与方另带 `X-Participant-Id`。生产环境应由网关注入。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/ledger/root` | 账本哈希根与事件数 |
| POST | `/events` | 提交事件（只追加；重复 `event_id` 幂等） |
| GET | `/events` | 机构浏览事件流（敏感字段按角色打码） |
| GET | `/periods/{id}/bundle` | 期依据包：锁定版本、证据、账户与流水（机构角色） |
| GET | `/periods/{id}/flows` | 四类流水 |
| GET | `/participants/{id}/evidence-pack` | 参与方下载本方完整依据（仅本人） |
| GET | `/accounts/{id}` | 参与方账户（本人或机构） |

提交事件示例（监测更正）：

```bash
curl -s -XPOST localhost:3000/events \
  -H 'X-Role: monitoring_body' -H 'Content-Type: application/json' \
  -d '{
    "event_id": "monitoring-2025-v2",
    "type": "monitoring.corrected",
    "payload": {
      "period_id": "2025",
      "allocatable_amount": "6000.00",
      "method_version": "M-2023-1",
      "baseline_version": "BL-V1",
      "boundary_version": "BND-V1",
      "evidence": [{"ref": "recheck.pdf", "sha256": "…"}],
      "reason": "复测调减"
    }
  }'
```

更正后由办公室逐方发起 `trueup.raised`（建议差额见期包 `trueup_suggestions`）：负差额先冲剩余应付，超出已付部分形成追补责任，以 `direction=return` 的支付流水返还。

## 代码结构

```
service/ledger/events.py      事件类型、提交角色、敏感字段清单
service/ledger/store.py       只追加存储、哈希链、幂等、JSONL 持久化
service/ledger/engine.py      写入校验与只读投影（账户/追补建议/流水/依据包）
service/ledger/visibility.py  分角色字段裁剪（不重算，只过滤）
service/api.py                HTTP 路由
scripts/demo_settlement.py    端到端演示
tests/                        锁定规则、结算流、存储、可见性、HTTP 测试
docs/domain.md                领域约定与账户口径
```
