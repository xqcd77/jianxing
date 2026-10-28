# 模型示例更新（2026-10-27）

模型菜单、厂商配置的默认型号和预设列表按官方资料更新。自定义型号和用户已保存的选择继续保留；“获取可用模型”仍从该账户的厂商接口读取实际权限列表。本次仅核对官方资料并运行本地模拟验证，没有调用付费推理接口。

| 厂商 | 当前默认 API ID | 界面补充 |
| --- | --- | --- |
| DeepSeek | deepseek-flash | 显示 DeepSeek-V4.1-Flash-1017；调用保持官方最新模型别名 |
| GLM | glm-5.3 | 替换旧的 glm-4.7 默认示例 |
| MiMo | mimo-v2.6-pro | 最新修复版本调用 ID 保持不变 |
| Kimi | kimi-k3.1-pro | 同时提供 kimi-k3.1-pro-highspeed |
| Qwen | qwen3.8-max | 替换旧的 qwen-plus 默认示例 |
| MiniMax | MiniMax-M3.1-Flash | 保留当前最新 Flash 系列默认值 |

GLM-5.3 与 Kimi K3.1 Pro 始终推理，不能沿用旧型号的关闭思考参数；适配层对这些确切型号使用 low 推理强度，保持现有单轮结构化提案和预算限制。旧型号调用参数继续按旧规则处理。DeepSeek 的版本展示标签与 API ID 分开，不能将展示名误当成调用 ID。

官方依据：

- [DeepSeek API 首次调用与最新别名](https://api-docs.deepseek.com/api/responses)
- [GLM-5.3 官方文档](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3)
- [MiMo 最新修复版本与调用 ID](https://mimo.xiaomi.com/zh/blog/mimo-v2-6-tool-call-repetition)
- [Kimi K3.1 Pro 官方说明](https://www.kimi.com/resources/kimi-k3-1-pro-pricing)
- [Qwen3.8-Max 模型信息](https://help.aliyun.com/en/model-studio/qwen3-8-max)
- [MiniMax 官方发布记录](https://ir.minimax.io/news-events/new-releases)
