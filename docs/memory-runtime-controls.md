# 记忆冲突、Token预算与运行指标

2026-10-27 增加[后台整理与读写策略](memory-curation-policy.md)：归档替代历史上限删除、精确去重、按需读取、关键偏好强制保留及整理状态查询。以下原文保留上限说明以该更新为准。

## 记忆冲突规则

余额、当前持仓、风险状态和授权仍由正式银行服务判定。历史回执作为当时的证据保存；本轮明确要求可以修订草案和偏好，办理参数变更仍需重新核对授权。

可更新偏好使用明确的`key`。更新范围由用户、scope、会话和角色集合共同确定，不用Embedding相似度猜测覆盖对象。未提供key的自由文本继续追加保存；检索相似不能成为改写旧原文的理由。

例如账单展示偏好从文字改为表格：

```json
{"text":"偏好详细文字账单。","domains":["bills"],"key":"report_style","expected_version":0}
```

向`POST /api/memory/preferences`提交后，通过`GET /api/memory`查看ID、key、version、status及superseded_by。修改时提交当前版本：

```json
{"text":"改为简洁表格账单。","domains":["bills"],"key":"report_style","expected_version":1}
```

新记录为version=2，旧记录标记superseded，正常检索只返回有效版本。旧记录仍可通过原文回读接口访问，返回原文、来源、SHA-256及替代关系，受原有归属、权限、有效期和历史保留上限约束。两个并发修改都基于version=1时，只有一个能成功；另一个收到409 `MEMORY_VERSION_CONFLICT`，需要重新读取版本。相同内容的重复提交返回已有记录。

版本检查和写入在同一SQL写事务内完成。轻量`memory_head`单据保留已使用的最高版本号，过期或删除后新增也不会复用旧版本号，避免过期客户端误覆盖。过期或删除后无有效当前值时，可使用expected_version=0创建。

产品快照按准确产品ID分组，旧快照继续保留，检索选取当前版本。若来源提供整数version，则拒绝低版本覆盖，且相同来源版本的矛盾内容需核对后再写入。旧数据没有冲突元信息时，只按精确产品身份及已有时间/版本排序，不做语义合并。同一记忆ID不能改写另一份原文；银行回执始终回读正式回执。

## 完整请求与累计预算

预算在Harness调用模型前执行，SDK和legacy_http使用相同检查。统计整个服务商请求JSON，包括所有messages、系统提示、用户输入、历史、记忆、工具结果、工具定义、JSON Schema及其他服务商参数；另外预留最大输出和安全余量。

```text
完整输入Token估计 + 最大输出预留 + 安全余量 ≤ 配置的上下文容量
```

默认配置：

```dotenv
BANK_MODEL_CONTEXT_TOKENS=131072
BANK_MODEL_TOKEN_SAFETY_MARGIN=2048
BANK_TASK_TOKEN_BUDGET=1048576
```

这是项目的可配置阈值，部署时需要按实际模型的上下文容量设置。当前计数器使用完整JSON的UTF-8字节数加协议开销进行保守估计，事件中标记`utf8_conservative_estimate`。它不是服务商精确Tokenizer，也不是账单Token；服务商返回的usage另行记录，缺失保持null。

超过上下文预算时，优先移除可省略的检索背景；有结构化任务状态时，可裁剪较早的非关键对话。保留最近对话、完整本轮请求、系统规则、工具Schema、明确偏好、金额日期、否定限制、保护字段及必须回读的证据。仍不能容纳时返回422 `MODEL_CONTEXT_BUDGET_EXCEEDED`，不截断必要资料，不发送本次模型请求。压缩前后估计量和裁剪数量记录在budget事件中。

同一服务端task_id中的多个角色还共享累计预算。每次调用先在SQL中原子预留“输入估计＋最大输出”，避免两个并行角色都误以为额度充足。调用结束后，有服务商total_tokens时按报告值结算；未发送的调用释放预留；已进入调用但usage未知时保留保守预留。超出累计限额返回422 `TASK_TOKEN_BUDGET_EXCEEDED`，停止后续调用。重复结束事件不能重复释放额度。

累计账本使用`runtime_budget`单据，仅含运行ID、计数和结算标记。预算按服务端任务上下文分组：研究使用稳定研究任务ID，角色并行和研究恢复共用该任务额度；办理和续办使用当前服务入口绑定的任务ID。它不充当账户级消费限额。既有交易回执和已经完成的业务步骤保持原状态。

## 统一事件

`runtime_events`是本地追加事件表，记录seq、event_id、request_id、task_id、run_id、角色、事件类型、时间及白名单元数据。

| 事件 | 含义 |
| --- | --- |
| request.started / request.finished | 已认证API请求开始、响应体发送结束或取消 |
| run.started / run.finished | 一次模型提案执行的开始和最终状态 |
| model.requested / model.responded | 服务商调用尝试与响应状态 |
| budget.checked / trimmed / rejected | 完整Prompt检查、裁剪与阻断 |
| budget.task_reserved / task_settled | 任务额度预留、结算 |
| memory.written / searched / reread / forgotten | 记忆写入、检索、回读与删除 |
| memory.write.rejected | 版本冲突、不可改写等拒绝 |
| agent.routed / tool.read.completed | Supervisor分流、只读工具取证 |
| workflow.transition / started / finished | 银行业务阶段迁移、后台研究协程运行 |
| business.audit | 其余业务审计的白名单元信息 |

业务审计与对应运行事件在同一个银行SQL事务中提交；事务回滚时，运行事件也回滚。workflow.finished表示后台协程结束，研究是否可用继续看研究单据的READY、BLOCKED、CANCELLED等业务状态。

事件只接收白名单字段，不复制提示词、思考过程、请求体、凭证或原始异常文本。SDK远程Tracing仍关闭。每个HTTP响应附带`X-Request-ID`；同一任务中的模型与业务事件可以据此关联。监控接口自身不写请求耗时事件，避免查询指标不断增加指标样本。

## 查询与指标口径

以下接口都需要登录，严格按本人过滤：

- `GET /api/runtime/runs`：最近50条模型提案记录。
- `GET /api/runtime/events?after=0&limit=100&task_id=...`：顺序事件与next_cursor，limit最大200。
- `GET /api/runtime/metrics?hours=24`：最近1—168小时的模型、HTTP、后台流程、预算与记忆指标。

模型指标包括尝试数、调用状态、服务商请求尝试数、已完成调用的平均/P95耗时、按角色耗时、服务商报告的Token总量、已知与未知usage次数、错误代码分布。预算拒绝也形成run记录，但没有model.requested事件，不计为服务商请求尝试。

HTTP耗时从服务端进入请求到发送完最后一段响应体，包含流式输出时间；后台研究耗时由workflow事件单独统计。P95使用最近秩方法。每个完整HTTP请求还按关联模型调用汇总报告Token；只有该请求关联调用的usage全部已知时才计入已知均值，没有模型调用的请求计0。异步研究消耗归属于触发请求ID，耗时与HTTP返回耗时分别呈现。

预算指标中的estimated_input_tokens与actual usage分开；未知usage不填0、不混入“已知调用平均Token”的分母。记忆指标展示检索次数、命中条数、回读次数和版本冲突次数。角色名称统一为supervisor、bills_research、wealth_research、business、verifier，legacy模式保留旧角色名称。

## 验证记录（2026-10-23）

- 全量离线回归：747通过、4项外部模型/Embedding测试未选取，1项既有冻结评测资料哈希校验失败。该问题与前一轮一致，相关资料及哈希清单未修改。
- 补齐累计预算与最终边界后，记忆冲突、完整请求预算、SDK Harness和统一事件专项：36通过；此前包含协议恢复、研究取消与动态编排的专项62通过。
- Ruff与Python编译检查通过。测试采用隔离临时数据库、合成服务商响应和本机Chroma，没有使用真实模型密钥或改写运行中的银行账户数据。
