# 工作台：模型选择、历史会话与上下文容量

更新于2026-10-26。沿用工作台现有纸色与墨绿主题，新增输入框模型选择、历史会话抽屉、上下文容量与任务记忆查看。本轮不涉及第二批长期记忆整理或新的模型压缩服务。

## 界面与使用

- **模型选择**：输入框左下角点击厂标与模型名，提供DeepSeek、GLM、MiMo、Kimi、Qwen、MiniMax六家入口，所有厂商均可打开设置，不显示“未接入”或禁用选项。设置中可填写API Key、选择预设型号、获取该Key可用模型，或手动填写模型ID，保存后用于后续请求。厂标是本地SVG。
- **历史会话**：侧栏与移动端导航可打开历史抽屉，支持新建、搜索、重命名、归档、恢复和分页读取更早消息；打开会话恢复原消息、当前计划与研究结果。已有任务记录与操作凭证入口保留。
- **上下文容量**：模型名旁的小圆环表示当前所选模型最近一次调用的输入占用。点击查看窗口、实际输入或估算、预算估算、输出预留、安全余量与可用预算。
- **任务与记忆**：容量面板可打开当前目标、进度、保留要求和适用历史记忆的来源查看。办理参数和权限仍来自正式任务、银行事实与原始证据，历史记忆不构成授权。

切换模型影响后续请求。打开新会话或其他历史会话只停止旧任务在前端的进度订阅，不取消后台任务；已有任务可以从历史记录重新打开。需要停止业务时，仍使用原有停止或取消入口。

## 服务商配置

默认沿用现有`BANK_MODEL_*`；六家各提供一个可由服务端修改的模型配置，界面展示实际配置的模型ID，不宣称这是各厂商最优或最新的模型。已有评测模型不因新增菜单而自动变化。

| 入口 | 默认模型ID | 服务端密钥配置 |
| --- | --- | --- |
| DeepSeek | 当前`BANK_MODEL_NAME`，示例为`deepseek-flash` | 当前`BANK_MODEL_API_KEY`；也支持`BANK_DEEPSEEK_API_KEY` |
| GLM | `glm-4.7` | `BANK_GLM_API_KEY`或`GLM_API_KEY` |
| MiMo | `mimo-v2.6-pro` | `BANK_MIMO_API_KEY`或`MIMO_API_KEY` |
| Kimi | `kimi-k2.5` | `BANK_KIMI_API_KEY`或`MOONSHOT_API_KEY` |
| Qwen | `qwen-plus` | `BANK_QWEN_API_KEY`或`DASHSCOPE_API_KEY` |
| MiniMax | `MiniMax-M3.1-Flash` | `BANK_MINIMAX_API_KEY`或`MINIMAX_API_KEY` |

每家还支持`BANK_<厂商大写>_MODEL`、`_BASE_URL`和`_CONTEXT_TOKENS`，例如`BANK_QWEN_BASE_URL`。按账号所在地域、实际开通型号及应用预算配置；默认应用窗口131072 Token，不自动宣称使用了厂商允许的最大窗口。修改配置后重启应用。`BANK_JUDGE_*`不会被自动用作业务密钥。

个人设置优先于同厂商的默认配置。每个厂商都能打开Key输入与型号选择；没有个人Key时，已提供的默认Key可以继续使用。Key不回填，已保存时留空保留原Key；填写新Key则替换本人该厂商的配置。不能提交自定义网络地址。

“获取可用模型”只读取厂商模型目录，不发起文本生成；部分厂商或账号不提供目录时，仍可用预设或手填模型ID。保存表示设置已写入，不代表已经验证额度或所有型号的业务表现；选择的型号须支持当前OpenAI兼容文本／结构化调用方式。

### 个人Key与专用接口

- `GET /api/models/{provider}/config`读取本人设置和具体型号选项，不返回Key。
- `POST /api/models/{provider}/config`提交`model`与可选`api_key`，只修改当前认证账户。
- `POST /api/models/{provider}/options`使用新填写或已保存的Key读取厂商目录，不因获取列表而保存新Key。

Key由现有Fernet加密服务加密保存，密文绑定用户和厂商；不写入会话、提示词、浏览器存储或运行指标。配置接口的请求校验错误只返回固定说明，不回显含Key的输入。输入框可临时显示／隐藏，关闭或保存后清空。

个人Key按版本保存。已排队或运行中的请求固定原Key版本与型号，更换配置只影响后续请求，防止旧任务在半途改用新Key。默认服务端Key保持原配置，不向浏览器披露。没有远端Key的确定性规则路径继续保留。

## 请求与存储边界

`ModelRegistry`只接受登记的厂商ID，具体型号由本人设置选择。每个后台请求创建时，将厂商、型号、窗口和Key版本写入本人`chat_model`文档；请求领取后放入`TaskContext`，通过上下文变量传给业务、研究与核验调用。共享适配器按本人和版本读取设置，不修改全局模型配置。重复请求标识不能被用来换模型再次提交。

模型输入继续经过上一批的`ContextBuilder → TaskBrief → 完整请求预算 → Agents SDK`。每次调用按对应窗口做预算；服务商参数、鉴权头和输出字段由统一适配层处理。新增服务商采用有界结构化调用；不支持的思考或格式参数不会照搬DeepSeek协议。MiniMax可能返回的思考前缀仅在本地解析时丢弃，不写入消息、日志或交给其他角色。银行授权、预检、幂等与资金网关保持原职责。

会话元信息只保存名称、归档状态、所选模型和时间；**历史消息复用已有ChatTask持久记录**，不从Chroma反推办理事实。旧UI任务可以直接进入列表，旧同步API只能回看此前实际保留的短对话，无法恢复已经未保存的文字。分页、模型选择、上下文、研究报告与记忆原文都按认证用户和会话读取；归档不会删除或重新执行业务。

研究任务可能在聊天请求结束后完成。历史查看时回读本人同会话的正式研究报告，避免一直显示“正在研究”；不跨用户引用报告。计划卡片同样重新查询当前状态，历史消息本身不作为最新余额或授权证据。

## 容量数字的含义

- 有提供商输入Token记录时，显示其返回用量；否则显示现有UTF-8保守估算，并明确标识。
- 圆环展示最近一次调用的输入占用，**不累计不同轮次或不同Agent的输入**。多Agent各自拥有独立请求窗口，这不是全体角色共用的一块上下文。
- “可用预算”按保守估算扣除输出预留和安全余量；实际输入用量单列，避免把更小的实际用量当成服务端可以额外接收的保证。
- 新会话、纯规则请求和刚切换到尚无调用记录的模型，显示“等待首次调用／发送需求后更新”；不伪造0 Token的提供商记录。
- 历史消息完整存档不等于全量发送给模型；下一次输入仍按正式任务状态、必要证据和预算重新装配。

## 验证

原有18项工作台专项测试保留；新增13项个人Key测试，覆盖加密与重载、账户隔离、实际传输使用本人Key和型号、跨账户并发、原Key版本固定、留空保留、错误不回显及读取模型目录。测试使用合成HTTP响应，没有调用付费接口，不能据此声称任意用户Key或全部型号已完成真实服务联调。

默认规则路径保持兼容：没有远端密钥时，信息完整的确定性业务仍可核对；确实需要模型才由适配器检查密钥。已有模型截断恢复流程继续使用原来的有界修复。用户询问模型身份时按会话选择与服务端配置回答，不向模型询问自身身份。

三组浏览器脚本通过12组场景，含1440px桌面与390px移动端、六家配置表单、密码显示／隐藏、获取型号、保存与刷新恢复、Key不回填、账户隔离、历史管理、旧任务不中断和容量查看，无页面脚本错误。截图、Key和模型目录来自独立合成夹具，不是正式评测或主账户实测。

主工作台重启后另做只读浏览器验收：六家入口全部可点开，均有空白密码输入和具体模型选择，无禁用项或“未接入”文案，历史抽屉与容量面板正常。只读验收不保存主账户Key，不发起资金操作或付费模型调用。

本轮完整离线回归：**985通过、4跳过**（989项，768.72秒）。Ruff、两套TypeScript类型检查和`git diff --check`通过。报告保存在本地`.data/personal_models_full.xml`，13项个人Key测试报告为`.data/personal_models_unit.xml`。浏览器结果在`.data/workspace-controls-browser.json`、`.data/workspace-task-switch-browser.json`和`.data/personal-models-browser.json`。历史58题冻结分数不随本轮改动更新，本轮没有付费模型评测。

复现界面测试时，从项目根目录运行独立预览，避免使用主账户：

```powershell
.venv\Scripts\python.exe -m scripts.workspace_controls_preview
$env:NODE_PATH = '<提供playwright的node_modules目录>'
node scripts/workspace_controls_smoke.cjs
node scripts/workspace_task_switch_smoke.cjs
node scripts/personal_models_ui_smoke.cjs
```

预览监听8140端口，所有模型凭证和目录都是合成夹具，`model_mode=demo`，不发送付费模型请求。单元测试入口为`pytest tests/test_workspace_controls.py tests/test_personal_model_settings.py`。

## 参考与图标来源

- [Codex App Server：会话、模型参数与Token用量事件](https://learn.chatgpt.com/docs/app-server)
- [DeepSeek官方接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/)
- [智谱GLM-4.7官方说明](https://docs.bigmodel.cn/cn/guide/models/text/glm-4.7)
- [MiMo官方Chat Completions接口](https://mimo.mi.com/docs/zh-CN/api/chat/openai-api)
- [Kimi官方OpenAI兼容接入](https://platform.kimi.com/docs/guide/migrating-from-openai-to-kimi)
- [Qwen官方Chat Completions说明](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [MiniMax官方OpenAI兼容接入](https://platform.minimax.cn/docs/api-reference/text-openai-api)
- 本地厂标来自[Lobe Icons](https://github.com/lobehub/lobe-icons)，`@lobehub/icons-static-svg` 1.101.0，保留`static/providers/LICENSE`。
