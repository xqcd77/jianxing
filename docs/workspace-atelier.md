# 工作台光影、粒子与中文排版升级

本轮只升级工作台及办理详情的视觉层。已确认的全屏登录页继续使用原来的布局、字体和2秒流光按钮。

工作台以青绿、暖白和低饱和玉色为主：深玉头图承载立体浮雕与动态轨道，玻璃卡片通过内侧反射、底部厚度和分层投影形成景深。大多数边界采用淡出柔光；对话与研究区域叠加低饱和青紫、珍珠粉的锥形流光，边缘粒子沿圆角路径流动，再向外消散。装饰不接收鼠标事件，不覆盖正文或替换账户数据。

粒子使用本地Canvas渲染，不依赖第三方动画包。桌面限制到约30fps、手机约24fps，设备像素比最多1.5，背景粒子与消散粒子有数量上限；只描绘当前视口中的少量主要卡片。隐藏页面、登录和办理弹窗会暂停渲染，系统减少动态效果会关闭动画。右上角的星形按钮可关闭/开启工作台动态并保存选择。

针对反馈截图，正文、报告、任务、按钮与数字统一使用本地化的Noto Sans SC派生子集；品牌及头图使用Noto Serif SC派生子集。取消混用宋体正文、浏览器默认summary字号和合成粗体，重新统一字号、行距及真实可变字重；需求标题与状态分开排列，金额使用等宽数字。字体由原OFL文件派生并改名，许可和来源保存在`bank_agent/static/fonts/`，约1.9MB正文字体与87KB展示字体不依赖运行时CDN。

字体来源及许可：[Noto Sans CJK](https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE)、[Noto Serif CJK](https://github.com/notofonts/noto-cjk/blob/main/Serif/LICENSE)。`scripts/build_workspace_fonts.py`用于重建子集，需要FontTools与Brotli构建工具；应用运行无需安装这些工具。

验收：

- `scripts/workspace_ui_smoke.cjs`在1440/390宽度验证实际画布像素变化、帧数/粒子上限、弹窗暂停、动态开关与持久化、减少动态效果、实际自托管字体渲染、任务/报告排版和无横向溢出；登录视觉不被工作台字体替换。
- `scripts/conversation_ui_smoke.cjs`九项交互全部通过。
- `scripts/multigoal_ui_smoke.cjs`桌面与手机的多任务转账、验证、测评返回与无重复办理检查通过。
- 两份TypeScript配置、字体构建脚本的Ruff检查通过。

截图与原始浏览器结果保存在本地`.data/atelier-*`及`.data/workspace-ui-results.json`；汇总见`workspace-atelier-verification.json`。全部交易验证使用隔离演示账户。本轮未改模型、资金规则、工具协议或数据库结构，未重跑全量后台回归。
