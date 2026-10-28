# 第二批上下文改进：角色返回协议与阶段去重

实施于 2026-10-26～27。沿用现有 Supervisor＋四个子 Agent、LangGraph、Agents SDK 和共享 Harness。

## 全链路

```text
用户请求 → SQL 最新任务状态 / TaskBrief
  → Supervisor 提取约束、按需分派
  → 账单 / 理财 Agent 隔离取证
  → 来源哈希、读取权限、结论可复现检查
  → RoleReturn：结论、来源引用、缺项、下一步
  → Supervisor 汇总 → 独立核验 → 报告及原始证据回读
  → 用户选择 → 业务草案 → 规则服务补全 → DAG 校验
  → 业务 RoleReturn → 独立核验 → 用户确认 / 分级授权 → 网关执行

每次模型调用：按角色选上下文和工具 → 阶段无损去重 → 完整请求预算检查 → SDK 调用
```

## RoleReturn v1

由服务端在原有 Pydantic 输出校验和程序校验之后组装，模型仍输出原有角色 Schema。交接包不要求模型重新生成来源、权限或授权状态。

| 字段 | 内容与用途 |
| --- | --- |
| 归属 | 用户、会话、任务、角色、阶段和轮次 / 修订号 |
| 状态 | complete / partial / blocked；待补和核验失败保留 |
| content | 按类型校验的分派、研究约束、研究结论、汇总、业务 Intent / 节点或核验结果 |
| evidence_refs | 已读取证据 ID、哈希、业务时间与原始来源 ID；结论不能引用未读取来源 |
| open_items / next_reads | 缺项、失败原因及角色允许的补查工具 |
| source_fingerprint | 研究基准或当前业务节点摘要；交接对象变化时不能沿用旧包 |
| authority | 固定为 proposal_or_read_only_not_authorization，不能作为交易授权 |

协议存入 SQL 的 `role_return` 文档，内容摘要作为 ID，读取时重新校验内容。摘要用于一致性检查，不是权限认证或数字签名。内部读取还检查用户、会话、角色和预期任务；研究汇总检查研究基准，业务核验检查规则服务补全后的节点版本。Supervisor 与核验角色可读取结构化公共交接结果；研究角色只能读取主管与本角色交接，不能读取另一研究角色的内部历史。

研究子 Agent 采用字段白名单接收任务与本领域证据；不传递对方的 results、candidate、历史推理或完整图状态。交接协议也不保存 Prompt、私有 rationale 或隐藏推理。研究记录只保存交接 ID 索引，现有原文回读接口继续读取受权限限制的正式来源；UI 不直接暴露交接文档。

业务规则服务会补全收款人与产品绑定，因此独立核验消费的是 **规则服务补全、DAG 校验后的节点交接**。初稿与补全稿分别留存，修订后更新交接引用。原有授权、资金预检、幂等、UNKNOWN 结果核实和回执机制继续由网关负责。

## 阶段压缩的具体范围

本批采用 `StageContextPacker` 做结构化无损去重，不额外调用模型生成摘要。

1. 在角色完成工具选择、Schema 组装和协议修复后，检查最终模型输入 JSON。
2. 当前请求、TaskBrief、金额及否定约束、当前银行事实、候选业务节点保持原值。
3. 历史上下文、证据、来源文档、角色交接和候选结论中，完全相同的大段内容只保留一份，用本次 JSON 内的引用指向它。内容不同的来源版本分别保留。
4. 每个压缩包必须能完整还原原输入，并通过整包哈希检查；保留引用读取指令的成本后没有实际节省，则使用原输入。
5. 原文若含协议保留字段，不解释为系统引用，直接按原样发送。无效、越界、循环引用或摘要不符均拒绝使用。
6. 完整预算检查包含系统提示词、Schema、工具、去重引用说明及输出预留。去重后仍超预算则停止调用，不能再部分裁掉压缩包造成悬空引用。

SQL 正式任务状态和原始证据不被压缩替换。这里的引用仅在单次请求内有效，不是文件读取、远程操作或交易工具。

## 事件、测量与验证

- `role.returned`：只记录交接 ID、阶段、状态、版本及计数。
- `context.compacted`：记录 JSON 字节数前后、共享对象数、引用数及完整请求的 UTF-8 保守预算估算。没有去重收益时不发出该事件。
- `budget.checked`：继续记录实际送往服务商的完整请求预算；服务商返回的 usage 才是实际模型 Token 口径。

可复现的本地测量：

```powershell
.venv\Scripts\python.exe -X utf8 -m scripts.measure_stage_context
```

该脚本使用临时合成数据库，实际走演示研究流程，截获研究汇总请求后在传输前终止，不请求付费模型。此次一个样例的输入 JSON 为 26,538 → 24,904 字节；含系统提示词、Schema、TaskBrief、来源和引用说明的完整请求预算估算为 **36,448 → 35,243（减少 3.31%）**，无损还原成功，服务商调用为 0。结果写入 `.data/stage_protocol_measurements.json`。这是单样例、同一新协议去重前后的比较，不是实际计费 Token、延迟或整套评测的平均改善；短请求可能不需要压缩。

新增测试覆盖协议类型、来源哈希、原文回读、用户 / 会话 / 角色 / 任务隔离、缺项保留、无授权不得执行、无损还原、引用注入 / 环路、整数精度及实际 HTTP 请求与预算一致性；同时回归现有研究、业务与 TaskBrief 流程。验收记录见 `.data/stage_protocol_targeted.xml`、`.data/stage_protocol_architecture.xml` 与 `.data/stage_protocol_full.xml`。

2026-10-27 验收：全量离线运行 1,019 项，**1,015 通过、4 按配置跳过、0 失败**；收尾修正其他目标的待续办标记后，再运行覆盖新协议、压缩、TaskBrief、Supervisor 和复杂多目标的 **140 项专项检查，全部通过**（`.data/stage_protocol_final_targeted.xml`）。主项目 `bank_agent / tests / scripts / evaluation` 的 Ruff、两套 TypeScript 检查通过；服务空闲时重载本地 8000，SDK / Chroma 健康，模型配置、会话与容量界面只读浏览检查通过、页面无脚本错误。

本次未运行付费 58 题评测，未改写旧题集、旧裁判标准或历史成绩。协议与 Prompt 的真实模型效果应在新的冻结运行版本中验证。

实现入口：`bank_agent/role_returns.py`、`stage_context.py`、`supervisor_research.py`、`supervisor_banking.py`、`agents.py`、`token_budget.py` 与 `runtime.py`。
