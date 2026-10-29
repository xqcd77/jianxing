# 简行部署说明

## 1 一条命令启动

需要 Python 3.12 或以上、Git，以及首次安装依赖时的网络连接。网页运行不需要 Node.js 或前端构建。

```powershell
git clone https://github.com/xqcd77/jianxing.git
cd jianxing
python run.py
```

macOS / Linux 使用 `python3 run.py`。已下载源码时，直接在源码目录运行最后一条命令。

首次运行自动创建 `.venv`、安装项目依赖并生成 `.env`，已有配置不会覆盖。后续运行直接启动服务。访问 http://127.0.0.1:8000 ，API文档为 http://127.0.0.1:8000/docs ，健康检查为 http://127.0.0.1:8000/health 。

演示账户为 `demo`、`wang`、`li`，密码均为 `DemoBank!2026`。初始主账户余额为0，登录后使用演示工具模拟入金。默认是规则演示模式，无需模型 Key。

```powershell
python run.py --port 8001
python run.py --setup-only
python run.py --update
```

以上分别用于更换端口、只安装环境、更新依赖。关闭服务按 Ctrl+C。

## 2 配置真实模型

在界面的模型选择器选择 DeepSeek、GLM、MiMo、Kimi、Qwen 或 MiniMax，填写自己的 API Key 和具体型号。配置保存在服务端，切换对下一次请求生效。也可修改 `.env`：

```dotenv
BANK_MODEL_MODE=llm
BANK_AGENT_RUNTIME=agents_sdk
BANK_ORCHESTRATION_MODE=supervisor
BANK_MODEL_BASE_URL=https://api.deepseek.com
BANK_MODEL_NAME=deepseek-flash
BANK_MODEL_API_KEY=填写自己的Key
```

模型名称可编辑，可用性及费用取决于厂商账户。账户、资金与实际执行仍然使用合成沙箱。

## 3 记忆检索

默认 `BANK_MEMORY_EMBEDDING=lexical` 可直接运行。要使用语义 Embedding，先准备缓存：

```powershell
.venv\Scripts\python.exe scripts/prepare_memory.py
```

macOS / Linux 使用 `.venv/bin/python scripts/prepare_memory.py`。完成后将 `.env` 的 `BANK_MEMORY_EMBEDDING` 改为 `fastembed` 并重启。lexical 是演示检索模式，不代表语义模型效果。

本地 Chroma 只由单进程持有。多 Web worker、独立任务进程采用下一节的 Chroma 服务。

## 4 Docker Compose

安装并启动 Docker 后，在项目目录执行。Windows PowerShell：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --build
docker compose ps
```

macOS / Linux：

```bash
test -f .env || cp .env.example .env
docker compose up -d --build
docker compose ps
```

访问 http://127.0.0.1:8000 。Compose 启动 Web、后台 worker、调度器和 Chroma，SQLite 与索引使用独立持久卷。默认容器检索为 lexical；语义模式需要另行准备模型缓存。

```bash
docker compose logs -f web worker
docker compose down
```

`down` 停止服务并保留数据卷。当前开发环境未安装 Docker，本轮已检查配置与服务关系，本机 Python 部署已实际启动验证。

## 5 开发检查

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check bank_agent evaluation tests run.py
npm ci --ignore-scripts
npm run typecheck
```

Node.js 只用于最后两条前端类型检查。模型评测默认关闭，复现步骤见 testing.md。

## 6 常见问题与备份

- 端口占用：使用 `python run.py --port 8001`，然后访问对应端口。
- 模型401/402：检查 Key、账户额度、接口地址和型号权限。
- Chroma目录占用：关闭其他实例，或使用 `BANK_CHROMA_URL` 连接独立服务。
- Embedding缓存缺失：先准备缓存，或明确使用 lexical。
- 任务中断：从历史会话检查未完成目标；资金提交结果未知时先核实回执。

备份前先停止服务，保留整个 `BANK_DATA_DIR`（默认 `.data`），包括 SQLite、检查点、auth.key 与索引。auth.key 和加密数据库分别保护，不上传 GitHub。源码归档不包含个人账户数据。

## 7 部署边界

默认监听本机127.0.0.1，使用合成账户和演示验证。公网部署前替换演示身份体系、关闭演示管理及验证码，配置TLS、限流、密钥管理与审计。SQLite与文件锁当前按单机协调设计，不保证跨主机一致性。
