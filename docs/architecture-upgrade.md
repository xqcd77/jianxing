# 简行架构改进与验收

本次对应用户选定的建议1、2、5、6、7、8、9、10。已有账本、授权网关、幂等回执和原文读取继续作为事实依据。此次没有将SQLite整体替换为其他主库，也没有额外引入Agent框架。

## 改动与验证入口

| 建议 | 已实现行为 | 主要验证 |
|---|---|---|
| 1 统一理解 | HTTP与Supervisor均进入RequestIntake；先保留源目标，再选择整句入口；模型遗漏目标会显示为待核对，不吞掉其他目标 | test_architecture_upgrade、test_complex_requests、test_request_goal_repairs |
| 2 任务状态 | 每个目标持久化ID、参数、状态、版本；目标修改API支持版本比较；自然语言支持序号、名称、金额调整和单项目取消 | test_architecture_upgrade：减少预算、纯资料目标、已完成不可重放、旧确认失效、跨用户拒绝 |
| 5 能力目录 | 网关定义派生统一目录；界面和模型使用同一权限/参数说明；按所有相关领域提供工具；外部ETF资料不开放本地理财替代工具 | test_architecture_upgrade、test_business_catalog、test_agents_harness |
| 6 进度与停止 | 请求持久化后返回任务ID；NDJSON阶段事件；慢资料查询前展示已生成计划；显式取消、重试去重、刷新恢复 | test_chat_tasks；桌面1440px与手机390px浏览器验收 |
| 7 记忆检索 | 用户/会话/角色过滤后覆盖保留窗口；写入后后台准备索引；保留SQL回读；特定明确请求可带标记降级，历史约束请求仍暂停 | test_memory_and_etf_upgrade、test_shared_memory、test_chroma_memory、test_memory_conflicts |
| 8 ETF资料 | 官方目录候选、允许的产品详情、带版本/读取时间/来源哈希的资料；核对风险、期限、渠道、金额和费用；未知字段保持空；界面展示差异与排除理由 | test_memory_and_etf_upgrade、test_public_information与浏览器展示 |
| 9 运行职责 | RequestIntake管请求；SDK单轮生成草案；LangGraph管流程；Harness管预算与指标；任务服务管租约/取消；网关管授权和执行 | runtime_policy、test_agents_harness、test_runtime_observability |
| 10 持久化与扩展 | chat_tasks表、租约领取/续租/过期恢复、独立Worker、增量迁移CLI、写锁等待统计；Compose分离Web/Worker/调度器/Chroma | test_chat_tasks：两个独立进程同队列只建一份计划；迁移与存储检查 |

## 调用与数据路径

```text
POST /api/chat/tasks（带client_request_id，202返回任务标识）
  → SQL任务队列 → 租约Worker → RequestIntake
  → 已有目标修改 / 明确规则 / Supervisor分派
  → 业务Agent受限提案（SDK）或只读研究
  → LangGraph校验、暂停与恢复
  → 银行网关和正式回执
  → 先展示已准备的办理部分，后补独立公开资料结果
GET /api/chat/tasks/{id}/events → NDJSON进度
POST /api/chat/tasks/{id}/cancel → 停止尚未完成的处理
```

`POST /api/chat`仍提供同步兼容接口，与后台任务共用RequestIntake。`GET /api/conversations/{id}/goals`读取本人目标，POST同一路径提交target_id、expected_revision、action和fields。聊天不产生授权凭证；修改会使旧的办理确认失效。

目标指代不唯一时要求明确目标，不能猜测。自然语言入口不承诺理解任意省略表达；结构化目标修改API使用统一状态和版本。多项明确修改应逐项提交，避免错误定位。已结束目标不可修改或重放。

ETF资料核对不等同于完成适合性评估，项目仍没有证券交易或商户会员开通通道。官方资料不提供的风险、费用、交易单位、价格日期或定投支持保持待核实。条件符合仅表示已披露字段与本轮条件没有冲突，不承诺收益，也不自动下单。产品数据测试使用明确的官方格式固定样例，不把样例数值当作实时市场数据。

实网验收另外记录在`architecture-upgrade-public-source.json`。官网目录中的旧详情链接会跳转到通用首页，因此优先接入官网页面实际使用的公开`getRateFund`接口；仅接受固定HTTPS主机、路径和单个六位产品代码，验证每条费率记录均属于该产品。管理费、托管费与分档申购费分开读取，冲突值保持未知；原始JSON证据、来源与读取时间一并留存。接口不可用时尝试允许的详情页，整个候选资料读取有3秒上限。它仍不能证明当前交易价格、起投金额或渠道是否支持定投。

## 测量口径与恢复

`/api/runtime/metrics`增加chat_tasks.first_usable和total，以及按完整任务归集的Token；缺失服务商usage仍为未知。HTTP按路由另分组，不能把202接收耗时或轮询耗时当成用户任务耗时。存储等待指标仅向演示管理员显示。

崩溃后不自动重放已开始的修改：它可能已经提交，重新执行“减少两百”会再次减少。过期任务保留INTERRUPTED状态和已有结果；可从计划历史核实。取消期间已提交的交易仍以正式回执为准；结果未知不伪称已撤销。

## 验证记录

逐点测试、全量回归、真实模型10+4专项和桌面/手机界面检查的最终结果记录在`architecture-upgrade-verification.json`。真实模型调用使用隔离临时账户、独立Chroma和本地Embedding缓存；公开资料使用固定样例，不修改现有用户账户。前端检查包含部分结果提前展示、目标金额调整、停止、刷新恢复与无横向溢出。

已有冻结评测包的checksum检查`test_frozen_files_and_full84_selection_provenance`在修改前就失败，本次不重写冻结数据或预期哈希。全量业务回归明确排除这一项以及默认不运行的外部模型/Embedding测试；外部模型专项单独运行并报告。
