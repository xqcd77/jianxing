# 配置与部署

## 本地开发

Python 3.12+，从仓库根目录安装。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\start.ps1
```

start.ps1默认使用单Web worker和进程内调度器，适配本地Chroma。配置`BANK_CHROMA_URL`后，可以使用多worker和独立调度器，退出时脚本结束自己启动的调度进程。浏览器访问127.0.0.1:8000。Linux/macOS使用.venv/bin/python；本地Chroma请设置`BANK_SCHEDULER_MODE=embedded`后启动uvicorn。

.env.example默认demo与lexical，便于无密钥启动。已有.env不会被自动修改；请自行复制模板并填写本地配置。演示账号和密码见README，均为合成账户。

当前默认编排为`BANK_ORCHESTRATION_MODE=supervisor`，对应1个Supervisor与4个子Agent；`legacy`只用于历史实现对照。更新已有安装时请显式核对该项，避免仍运行旧编排。

新增默认`BANK_AGENT_RUNTIME=agents_sdk`和`BANK_MEMORY_BACKEND=chroma`。已有原文不需要手动转换，首次授权检索时重建对应索引；迁移参数、回退选项和本地用量接口见[接入说明](runtime-migration.md)。

## 业务模型与Embedding

```dotenv
BANK_MODEL_MODE=llm
BANK_MODEL_BASE_URL=https://api.deepseek.com
BANK_MODEL_NAME=deepseek-flash
BANK_MODEL_API_KEY=本地填写
BANK_MODEL_THINKING=true
BANK_MODEL_REASONING_EFFORT=max
BANK_MODEL_MAX_TOKENS=16384
BANK_MEMORY_EMBEDDING=fastembed
```

运行python scripts/prepare_memory.py准备BGE缓存。离线时可显式使用lexical。截断、无效结构等仅作有界恢复；已有可靠字段也只是草案，不能签发授权或绕过网关。模型配置的可用性由实际服务商决定。

MiMo独立裁判通过BANK_JUDGE_*配置；真实模型与裁判调用会使用各自账户额度。不要把密钥放到README、截图或提交记录。

## 调度与多进程

本地Chroma目录由一个进程使用：单Web worker搭配`BANK_SCHEDULER_MODE=embedded`。需要多个Web worker或独立调度进程时，先启动Chroma服务并配置`BANK_CHROMA_URL=http://127.0.0.1:8001`；再设置`BANK_SCHEDULER_MODE=external`，单独运行python -m bank_agent.scheduler。API和调度器必须连接同一个Chroma服务，不能多进程同时打开本地Chroma目录。SQLite与文件锁仍适用于当前单机演示，不支持将同一方案直接宣称为跨主机一致性。

## Docker

复制.env.example为.env后执行：

```bash
docker compose up --build
```

Web、请求Worker与scheduler共享银行数据卷，并连接独立Chroma容器；向量索引使用自己的数据卷，Chroma端口不向宿主机发布。compose默认监听127.0.0.1:8000，设置两个Web worker，并通过`BANK_CHAT_WORKER_MODE=external`将请求交给独立Worker。需要Docker可用。可选的生成代码执行沙箱仍需单独构建docker/中的镜像与配置；不要向应用容器暴露宿主机Docker socket。

## 请求Worker与存储维护

单机默认`BANK_CHAT_WORKER_MODE=embedded`。请求进入`chat_tasks`后领取租约并保存进度，前端使用NDJSON事件；刷新可恢复查看。独立部署时，Web配置`external`，另运行`python -m bank_agent.worker`，双方必须共享SQL数据目录并使用同一Chroma HTTP服务。并发由`BANK_CHAT_CONCURRENCY`控制，队列上限由`BANK_CHAT_QUEUE_LIMIT`控制，租约默认30秒并续租。

同一用户和client_request_id只产生一个任务。崩溃后，未领取任务可继续处理；过期的RUNNING任务标记INTERRUPTED，保留已有计划，不能自动重放修改指令或资金操作。停止请求与撤销已提交交易分别处理；结果未知时继续保留核实入口。

`python -m bank_agent.maintenance status`以只读连接查看任务与迁移版本；`migrate`执行幂等的增量表结构/目标版本升级；`rebuild-memory`从SQL原文重建索引。迁移不删除原文、计划、授权或回执。管理员的`/api/runtime/metrics`包含SQLite写锁等待均值与P95，可据此判断后续主库迁移时机。当前实现仍是单机共享目录协调，不宣称支持跨主机分布式一致性。

## 前端检查

```bash
npm ci
npm run typecheck
```

应用前端是直接提供的ES modules，无需前端打包才能启动；npm用于类型开发检查。工作区锁文件保留已有Windows环境的版本记录，新平台优先按pyproject.toml安装。
