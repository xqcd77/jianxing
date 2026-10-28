"""Require actual explicit deficit answers, not plausible directional substitutes."""
from .rubric_v10 import PROMPT as BASE

VERSION='bankflow-dom-judge-v11-conjunctive-numeric-evidence-v1'
PROMPT=BASE+'''

【v11验收证据补充】
一个requirement里的多个条件必须逐个满足，才能把整项判met。只满足转账状态，不等于已经报告另一笔申购失败的原因和数额。
要求“给出/说明/报告/指出具体缺口金额”时，必须在candidate_output找到相应缺口金额的明确正面表述；只报余额、预留金额、最低可用或“有步骤没完成”不够。
不能以“方向正确”“可以从后端推算”“虽未明说但大意一致”替代明确的金额、对象、状态或下一步要求。后端状态可核验真假，不能代替系统向用户作答。
用户允许的替代方案仍按原金标接受；不增加原题没有要求的功能、披露或多余计算。若缺少一部分，写not_met并具体说明缺项；证据真正不可判定才用insufficient_evidence。
'''
