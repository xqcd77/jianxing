# BankFlowBench评测入口

2026-10-24 **Lite58＝原48题＋10道复杂业务专项** 审计与修复已完成：[三阶段对照与剩余问题](../docs/evaluation58-audit-2026-10-24.md)、[结构化记录](../docs/evaluation58-audit-2026-10-24.json)。修复版严格通过50/58（86.21%，未决不计通过），6题失败、2题未决；业务硬检查58/58。57份有效裁判的适用维度加权4.4873/5；复杂10题严格通过10/10。题目、gold、硬检查和2∶2∶1∶1∶1权重不变，v9修正只读取证、阶段判断与评分锚点。它是已参与开发的诊断集，不是独立盲测。

修复前v8原记录继续保留：[原报告](../docs/evaluation58-2026-10-24.md)、[原结构化结果](../docs/evaluation58-summary-2026-10-24.json)。原26/58、3.7023/5混合了折叠内容与续轮答复漏采、裁判算术错误及真实业务回归；同一旧业务输出按v9重新取证判分为43/58、4.3816/5，不能把全部变化归因为代码改进。

新入口示例（使用新的短目录名，避免Windows模型缓存长路径）：

```powershell
$env:BANK_JUDGE_THINKING='false'
$env:BANK_JUDGE_MAX_COMPLETION_TOKENS='4096'
$env:BANK_JUDGE_TIMEOUT='240'
$env:BANK_JUDGE_FORMAT_RETRIES='2'
python scripts/run_lite58_eval.py --root .data/evaluation/l58_new --workers 3
```

运行需要已准备的BGE缓存及业务/裁判密钥，会使用付费API；每个任务只复用不可变模型文件，账户、记忆和Chroma索引分别隔离。完成的结果按冻结文件复算，不覆盖有效失败后重复取高分。[58题契约](cases/bankflow_lite58/README.md)与历史48题分别保存。

下列为2026-10-19冻结的 **BankFlowBench-Lite48（10领域／48题）** 历史两组对比：Single-Agent与当时的Multi-Agent Deep Research。采用应用执行、冻结界面只读回放和MiMo v8裁判；展示严格通过率及五维加权评分，不代表迁移后当前版本的成绩。

[评测口径与结果](../docs/evaluation.md) · [结构化汇总](../docs/evaluation-summary.json) · [精简逐题记录](published/bankflow_lite48_20261018/)

## 无API离线复算

```bash
python evaluation/compare_results.py
python evaluation/compare_results.py --output docs/evaluation-summary.json
```

脚本仅依赖Python标准库，检查两组题集和裁判提示词哈希、案例一致性、评分范围及通过状态，再由逐题记录重算。评分权重为需求覆盖2、银行业务正确性2、证据推理1、可操作性1、清晰度1。

现有`python -m evaluation`和`python -m evaluation.servicebench`中的旧35/100/64题执行入口作为历史工具保留，不代替本次48题结果，也不能将历史分数混合汇总。当前组为Supervisor＋4子Agent；历史基线与当前组调用上限分别为10与16，属于版本对照。重新执行业务需要匹配记录中的冻结源码版本与对应题集，不能把离线复算表述为新一轮业务评测。

## 当前应用评测入口

先在本地环境配置业务模型和独立裁判密钥。发布结果使用`BANK_JUDGE_THINKING=false`、`BANK_JUDGE_MAX_COMPLETION_TOKENS=4096`、`BANK_JUDGE_TIMEOUT=240`、`BANK_JUDGE_FORMAT_RETRIES=2`；与通用`.env.example`默认值不同，复测时须显式设置。

以下命令新建独立运行目录；`run`与`judge`会调用付费API，不覆盖已发布记录：

```bash
python -m evaluation.servicebench run --suite lite48 --root .data/evaluation/lite48_reproduction --variants SUPERVISOR --mode llm --memory lexical --trials 1 --workers 3
python -m evaluation.servicebench capture --root .data/evaluation/lite48_reproduction --clean-dom
python -m evaluation.servicebench preflight --root .data/evaluation/lite48_reproduction --clean-dom --judge-revision v8
python -m evaluation.servicebench judge --root .data/evaluation/lite48_reproduction --clean-dom --judge-revision v8 --judge-concurrency 2
python -m evaluation.servicebench report --root .data/evaluation/lite48_reproduction --clean-dom --judge-revision v8
```

界面回放需要可用的Node.js与Playwright/Chromium环境；运行前检查[`capture_servicebench_dom_v2.cjs`](../scripts/capture_servicebench_dom_v2.cjs)依赖。重新运行受外部模型波动影响，不保证复现同一分数。
# Lite66 · 长期记忆与上下文扩展（2026-10-27）

在Lite58原题与金标不变的基础上，增加长期记忆4题、上下文理解4题。正式比较重新执行全部66题的Single-Agent与当前Supervisor架构，采用统一v12裁判协议，不拼接历史成绩。

- [题集说明与新增题验收](cases/bankflow_lite66/README.md)
- [新增8题逐题卡片](../docs/evaluation66-new8-cards-2026-10-27.md)
- [冻结、严格通过与五维评分协议](../docs/evaluation66-protocol-2026-10-27.md)
- [本轮完整结果与缺失审计](../docs/evaluation66-2026-10-27.md)

已授权真实模型评测时运行：`python -m scripts.run_lite66_compare --phase all`。正式产物保存在`.data/evaluation/lite66_20261027_v12`；本地私有轨迹与金标不可发送给业务模型。

