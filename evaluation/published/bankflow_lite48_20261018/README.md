# BankFlowBench-Lite48：两组精简评测记录

- `single-agent.json`：冻结Single-Agent基线的48条记录。
- `multi-agent-deep-research.json`：专项修复后Deep Research版本的48条记录。
- `protocol.json`：题集哈希、领域与难度分布、相同裁判协议和实验边界。

仅保存案例ID、难度、验收状态、硬检查和五维分数。原始报告SHA256、业务源码哈希与运行manifest哈希可用于回查本地冻结记录。文件不含账户数据库、密钥、原始请求或模型对话。

从项目根目录执行`python evaluation/compare_results.py`即可复算README中的严格通过率与五维加权评分。未决保留在通过率分母；不适用评分维度保持缺省，不补零或满分。权重调整不会改动原始评分或通过状态。

数据为开发诊断集、每题各1轮；两组对应不同业务源码版本，不宣称独立盲测或纯架构因果提升。详见[评测说明](../../../docs/evaluation.md)。
