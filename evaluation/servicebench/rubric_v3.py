"""Measurement clarification only; the atomic criteria and pass gates stay fixed."""
from .rubric import PROMPT as BASE_PROMPT

VERSION = "finservice-atomic-judge-v3-regions"
PROMPT = BASE_PROMPT + """
【界面区域与测量范围】
候选中的assistant_reply是本轮聊天回复；business_card是实际业务状态与结果；research_report是研究报告；error_notice是错误提示。
它们可以共同满足题目要求。聊天中已经直接回答的内容，不能因为旁边存在无关页面标签就判为没有回答。
同一source在1440与390宽度下是同一次回复的两个展示，不是助手重复追问两次；不得因此扣分。
页面常驻导航、欢迎语、空计划提示不属于助手对本轮用户的再次提问；不能据此推翻已出现的身份或知识回答。
确有业务卡片要求重复填写已知信息时，仍必须扣分；不能因为聊天文字正确就忽视卡片中的错金额、错误范围或死路。
每个not_met只能否定该criterion本身，不能在reason中否认候选已明确出现的事实。
遗漏风险披露或下一步属于覆盖不足；只有错误事实、误导性金融口径才标wrong_financial_fact。不要将一切缺项标成金融计算错误。
independent_ledger来源是隔离账户原始流水独立读取，不是模型编写的答案。核查分类、工资和退款时使用这些原始记录。
参考材料未列出某项细节，不能直接推断该细节系编造；真的无法核验时用insufficient_evidence。明确与来源冲突时仍为not_met。
订阅取消草案违背“别取消”也属于规划缺陷；区分提出了未授权操作与已实际取消，reason中准确说明发生在哪个阶段。
只输出严格schema JSON，所有reason简短。summary是顶层字段，不得放入scores；引用尽量少于80字，逐字复制。
"""
