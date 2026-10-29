# BankFlowBench评测入口

当前题集为10领域66题，最新当前架构64/66严格按预期处理，五维4.7431/5。执行类实际办结为17/22；两者口径不同。详见[结果与测量边界](../docs/evaluation.md)和[运行说明](../docs/testing.md)。

```powershell
python -X utf8 -m scripts.run_lite66_compare --root .data/evaluation/new_run --phase all
```

该命令默认执行Single-Agent和Supervisor两组，会调用付费模型。每次使用新目录冻结源码、题集、工具和评分协议，不覆盖有效低分。金标仅由控制器和裁判使用，不发送给业务模型。

当前主入口为cases/bankflow_lite66与published/bankflow_lite66_20261028。48题、58题及其引用数据是66题一致性检查与历史比较的依赖，不能因目录名较旧而删除。历史执行器不用于证明当前结果。裁判协议、逐题结果和失败均可追溯。
