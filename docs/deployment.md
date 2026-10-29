# 简行部署与使用说明

推荐先用本地规则演示模式确认界面与业务流程，再配置真实模型。规则演示无需API Key，不具有真实模型的开放语言理解能力。账户与资金始终是合成数据。

## 1 环境要求

Python 3.12或以上，联网安装Python依赖。Node.js仅用于前端类型检查，运行网页无需前端打包。Docker是可选部署方式。建议预留充足磁盘与内存，当前未做最低硬件基准测试。

## 2 Windows快速启动

在源代码目录打开PowerShell，依次执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\start.ps1
```

首次配置才复制.env.example，已有.env请先备份，不要覆盖自己的Key。若PowerShell阻止脚本运行，可在当前终端临时执行 `Set-ExecutionPolicy -Scope Process Bypass`，或按下一节直接启动Python。

浏览器打开 http://127.0.0.1:8000 ，API文档为 http://127.0.0.1:8000/docs 。这是本机地址，评委电脑需按上述步骤启动，不能当成公网演示链接。

演示账户demo、wang、li的密码均为DemoBank!2026。主账户初始余额为0，登录后使用演示工具模拟工资到账。只输入合成数据，不录入真实密码、银行卡、验证码或个人资金信息。

## 3 macOS Linux或直接启动

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
BANK_SCHEDULER_MODE=embedded .venv/bin/python -m uvicorn bank_agent.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Windows直接启动时先设置 `$env:BANK_SCHEDULER_MODE="embedded"`，再运行 `.venv\Scripts\python.exe -m uvicorn bank_agent.main:app --host 127.0.0.1 --port 8000 --workers 1`。

本地Chroma目录只能由单个进程持有。需要多worker或独立任务进程时，使用下一节的Chroma服务，不要让多个进程同时打开同一索引目录。

## 4 配置业务模型

修改本地.env：

```dotenv
BANK_MODEL_MODE=llm
BANK_AGENT_RUNTIME=agents_sdk
BANK_ORCHESTRATION_MODE=supervisor
BANK_MODEL_BASE_URL=https://api.deepseek.com
BANK_MODEL_NAME=deepseek-flash
BANK_MODEL_API_KEY=填写你自己的Key
BANK_MEMORY_EMBEDDING=fastembed
```

运行 `python scripts/prepare_memory.py` 准备本地Embedding模型缓存，再启动服务。这里的python需使用上述虚拟环境路径。也可显式设置BANK_MEMORY_EMBEDDING=lexical演示检索，该模式不等于语义Embedding效果。

模型选择器支持DeepSeek、GLM、MiMo、Kimi、Qwen和MiniMax。用户可填写自己的Key并选择具体模型。预设名称是可编辑示例，可用性、费用与额度取决于厂商账户和接口。已有任务绑定发起时的模型，切换对下一次请求生效。

## 5 Docker可选部署

```bash
cp .env.example .env
docker compose up --build
```

compose.yaml包含web、worker、scheduler和Chroma。只向本机暴露8000端口，账本数据和向量索引分别保存在数据卷。若启用fastembed，应在对应容器环境准备缓存。本轮检查了Compose配置，未实际启动Docker容器。

## 6 检查与停止

打开 /health 检查服务状态。关闭服务使用Ctrl+C；start.ps1会清理自己启动的调度进程。聊天中的停止按钮取消未提交工作，不撤销已提交业务。

```powershell
.venv\Scripts\python.exe -m bank_agent.maintenance status
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check bank_agent evaluation tests
npm ci --ignore-scripts
npm run typecheck
```

## 7 数据备份与常见问题

先停止所有服务，再备份整个BANK_DATA_DIR目录，尤其是SQLite数据、检查点、auth.key和索引。auth.key与加密数据库应按保密材料保护，绝不上传GitHub。不要将交付源码包理解为个人账户备份。

端口占用：停止占用8000的进程，或修改uvicorn端口后访问新端口。Chroma目录占用：关闭其他实例，或改用Chroma HTTP服务。模型HTTP 401/402：检查Key、额度和模型权限。Embedding缓存缺失：先准备缓存或显式使用lexical。任务中断：回到历史会话检查未完成目标，资金提交未知时先核实回执。

## 8 公网部署前提

默认配置用于本地演示。公网部署前关闭演示管理与演示验证码，替换演示身份体系，配置TLS、限流、密钥管理、审计与数据隔离。当前SQLite及文件锁方案按单机协调设计，不保证跨主机分布式一致性。
