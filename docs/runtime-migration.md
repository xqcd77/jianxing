# Agents SDK与Chroma接入

本轮将模型调用和向量检索替换为可统一管理的运行时与记忆服务。现有Supervisor、双研究角色、业务办理和独立核验均复用同一个Harness；工作流暂由LangGraph与AsyncSqliteSaver继续保存和恢复，业务账本、授权、原文和回执继续使用SQLAlchemy / SQLite。

后续已补上[记忆冲突规则、完整请求Token预算、统一运行事件与指标](memory-runtime-controls.md)。

## 调用链与权限

```text
已认证的服务入口
  → Harness：owner / conversation / task上下文、期限、取消、用量
  → Agents SDK Agent + Runner（一次提案最多一个模型回合）
  → ProposalModel：调用现有服务商协议适配器
  → Pydantic校验后的提案
  → 原工作流：核验、授权网关、幂等执行、回执
```

`ProposalModel`实现SDK的`Model`接口，保留原DeepSeek thinking、JSON输出、协议修复和HTTP错误分类。SDK不会额外再请求一次OpenAI模型，也不要求增加OpenAI密钥；仍使用`BANK_MODEL_*`配置。SDK不接收可执行的转账、申购等工具。角色提示词、只读取证工具目录和业务校验继续由现有服务管理。模型不直接决定是否得到交易授权。

每次提案执行使用`max_turns=1`，异常交回现有有界恢复逻辑，避免两层重试叠加。调用有总期限，异步取消会传播到HTTP请求；同步兼容入口中的HTTP线程只能等待其自身超时，因此线上异步入口是推荐路径。银行事务仍按原规则完成提交或回滚后才响应取消，已执行回执不会因取消而删除。

SDK远程Tracing逐次关闭。本地`agent_runs`表仅记录角色、任务、模型、状态、耗时、服务商Token计数和错误代码，不记录提示词、思考过程或原始报错。`GET /api/runtime/runs`需要登录，仅返回本人最近50次模型调用。服务商缺失usage时保存`null`，不伪造零；这里的耗时是模型提案执行耗时，不是整项银行任务或用户等待时间。规则短路径不调用模型，也不会产生模型调用记录。

## 记忆链路

```text
SQL来源记录：用户、会话、角色、有效期先过滤（检索覆盖保留窗口，不再截取最近80条）
  → 现有Embedding生成器：BGE或明确标识的lexical模式
  → Chroma：向量 + 来源/权限元数据，余弦距离排序
  → 用返回ID选择已授权SQL候选
  → TextRank提取原句，直接保护金额、日期、否定要求等字段
  → 超预算或核验需要时，按来源ID回读完整原文
```

Chroma只保存派生索引，不复制原文。原文回读继续返回来源ID、类型、版本和SHA-256；银行回执读取正式回执表，研究报告读取正式报告。索引中的文本或自然语言结论不能变成银行事实或授权。查询同时附带用户、会话、角色、有效期与候选ID过滤；返回ID还必须属于SQL筛选后的集合。

原有SQL向量缓存用于避免重复编码。模型ID和向量维度决定Chroma集合名；内容哈希和权限元数据变化会触发更新，避免混用不同模型向量。新写入在后台准备Embedding与索引，查询只为尚未准备的记录做补建，不再每次先读取全部索引元数据。旧记录可按需补建，也可运行`python -m bank_agent.maintenance rebuild-memory`重建。原文不需要改写。删除记忆会先删除所有相关模型集合中的向量，失败则返回错误，不提前宣称完成；正式银行回执仍按原规则保留。

Chroma服务不可用时，显式记忆查询与依赖历史的任务仍明确失败，不静默切回NumPy。仅完全明确、无历史指代、且没有相关有效否定约束的单笔转账或余额查询，可以由`search_for_request`返回带degraded标记的结果，保留实时SQL事实、保护字段和审计；对客说明历史资料暂不可用。若需要做历史对照，必须显式选择旧后端。

## 配置和部署

先更新依赖：`python -m pip install -e ".[dev]"`。默认值：

```dotenv
BANK_AGENT_RUNTIME=agents_sdk
BANK_MEMORY_BACKEND=chroma
BANK_CHROMA_URL=
BANK_SCHEDULER_MODE=embedded
BANK_WEB_WORKERS=1
```

留空`BANK_CHROMA_URL`时使用`BANK_DATA_DIR/chroma`本地持久索引，支持同进程内多个记忆服务实例。`start.ps1`将调度器放在同一进程中，避免跨进程同时操作嵌入式Chroma。

多worker或独立调度器连接Chroma服务：

```powershell
.venv\Scripts\chroma.exe run --path .data/chroma-server --host 127.0.0.1 --port 8001
```

另一个终端配置`BANK_CHROMA_URL=http://127.0.0.1:8001`再启动。Docker Compose已增加Chroma服务，Web与scheduler使用`http://chroma:8000`。正式SQL数据库与本地文件锁的单机约束不变。当前HTTP配置面向同机或可信内网，不包含云端身份认证与跨租户数据库配置。

显式回退：`BANK_AGENT_RUNTIME=legacy_http`、`BANK_MEMORY_BACKEND=numpy`。不要将`BANK_ORCHESTRATION_MODE=legacy`与模型运行时回退混淆：前者切换角色编排，后者只切换模型调用。`GET /health`显示实际runtime、memory_backend、index_mode及Embedding后端。

## 验证范围

新增测试使用真实SDK Runner、合成HTTP响应、真实本地Chroma及本机Chroma HTTP服务，覆盖类型约束、用量未知值、权限过滤、检索期间权限撤销、失败、超时、取消、持久化重启、索引重建、原文回读和向量删除。原银行回归集继续检查授权、交易幂等、暂停恢复与研究取消。本轮不使用真实密钥发起付费模型评测，历史BankFlowBench分数不能视为迁移后的模型成绩。

2026-10-23验证记录：

- 首轮全量离线回归：719通过，4项外部模型/Embedding测试未选取，1项冻结文件哈希校验失败。核对确认`evaluation/cases/bankflow_lite48`中的README与中文题卡在Git当前提交中就与既有哈希清单不符；本轮未改动这些资料与清单。
- 完成服务模式与二次权限校验后，补跑Harness、Chroma、共享记忆、Supervisor、研究恢复、动态编排等专项：120通过。
- Ruff、Python编译、PowerShell语法解析、Compose YAML解析通过。当前环境没有Docker，未实际构建或启动Compose；HTTP客户端已通过本机真实Chroma服务验证。

接口依据：[Agents模型接口](https://developers.openai.com/api/docs/guides/agents/models)、[Chroma客户端与服务模式](https://docs.trychroma.com/production/chroma-server/client-server-mode)、[Chroma Docker部署](https://docs.trychroma.com/deployment/docker)。
