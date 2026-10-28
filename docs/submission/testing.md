# 简行测试用例与验证说明

测试分为离线自动化、真实模型诊断评测和界面回放。三者验证的对象不同，报告时分别列出。

## 1 离线自动化测试

在源代码目录安装开发依赖后运行 `python -m pytest`。默认不调用收费业务模型或裁判。历史冻结版本为1099通过、4跳过、0失败；交付整理后重新运行的结果记录在测试记录目录，不能用历史记录冒充本次运行。

重点用例：

| 场景 | 测试文件 | 预期 |
| --- | --- | --- |
| 大额、旧授权及跨用户访问 | tests/test_safety.py | 无有效本次授权不得执行 |
| 金额与收款人变化 | tests/test_request_goal_repairs.py | 只修改对应目标，重算关联预算 |
| 记忆访问与冲突 | tests/test_chroma_memory.py、test_memory_conflicts.py | 不串用户，不恢复失效信息 |
| 长期记忆整理 | tests/test_memory_curation.py | 保留来源并正确失效、去重 |
| 任务交接与压缩 | tests/test_context_builder.py、test_role_returns.py、test_stage_context.py | 关键字段不因去重丢失 |
| 工具异常与恢复 | tests/test_protocol_repair.py、test_review_regressions.py | 有界恢复，不伪造成功 |
| 多目标与语义修复 | tests/test_lite66_repairs.py | 不丢目标、不重复交易 |
| 用户模型Key | tests/test_personal_model_settings.py | 配置隔离，响应不返回明文Key |

## 2 真实模型诊断评测

题集位于evaluation/cases/bankflow_lite66，基于原58题增加8道记忆与上下文题，业务领域仍为10类。public.jsonl描述任务，private_gold.jsonl用于评测控制器与裁判，不能加入业务模型上下文。文件名private指评测信息隔离，并不表示公开仓库不可读取。

```powershell
.venv\Scripts\python.exe -X utf8 -m scripts.run_lite66_compare --root .data/evaluation/my_run --phase all
```

该命令默认运行两套系统，需配置业务模型和MiMo裁判，消耗相应额度。也可通过 --variants SUPERVISOR 只运行当前架构，参数以 --help 为准。每个新运行目录独立冻结源码、题集与协议，不覆盖有效低分或拼接不同版本成绩。

最新现有结果：当前架构64/66严格通过，96.97%，五维加权分4.7431/5。规则状态66/66通过并不等于语义全部通过。本轮未重跑Single-Agent，因此不提供同版本对照提升结论。

## 3 严格通过与五维评分

严格通过同时要求资金、授权、状态及全部必需语义项通过。五个维度为任务覆盖、业务正确性、证据推理、可执行性、表达清晰度，权重为2:2:1:1:1。每维先计算适用且有效的平均分，再按权重汇总。N/A不补满分，裁判故障不计通过，保留总分母。

本轮模型为DeepSeek deepseek-flash，裁判为MiMo mimo-v2.6-pro，协议v12。题集参与开发，单次运行存在随机性，不能解释为独立盲测或生产可靠性。

## 4 界面回放与原始证据

冻结诊断包含66份桌面1440px与手机390px回放，回放阶段阻止银行API写入，不重做交易。交付包含脱敏汇总、逐题状态、JUnit记录和一份合成案例证据。完整运行目录含本地缓存、截图和临时运行状态，不直接公开。复现可使用源码与题集新建隔离沙箱。

## 5 构建与发布检查

发布检查覆盖公开文件密钥扫描、README本地链接、静态资源随wheel分发、前端两套TypeScript类型检查、源码压缩包和交付文件SHA256。测试和构建不替代独立安全审查。
