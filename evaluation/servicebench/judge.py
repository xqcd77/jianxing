"""Task-specific MiMo judgement; original v1 protocol and historical scores stay intact."""
import asyncio
import hashlib
import json
import re
import time
from decimal import Decimal
from typing import Literal

import httpx
from pydantic import Field, StrictInt, ValidationError

from evaluation.llm_judge import JudgeError, MiMoJudge
from evaluation.schemas import EvalModel
from evaluation.trace import scrub

from . import rubric


class Reference(EvalModel):
    id: str
    authority: Literal["independent_oracle", "bank_snapshot", "public_fixture", "runtime_config", "research_source"]
    text: str = Field(min_length=1, max_length=16000)


class Packet(EvalModel):
    # IDs, model names, variant labels and historical scores are intentionally absent.
    user_goal: str
    dialogue: list[dict]
    expected_behavior: str
    acceptable_alternatives: str
    candidate_output: str = Field(max_length=50000)
    requirements: list[dict]
    evidence: list[Reference]
    evidence_required: bool
    applicable_dimensions: list[Literal["coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity"]]
    observation_scope: Literal["api_response_text", "browser_dom_replay", "browser_dom_regions_v2", "browser_dom_regions_v3", "browser_dom_regions_v4"]


class Finding(EvalModel):
    criterion_id: str
    verdict: Literal["met", "not_met", "insufficient_evidence"]
    candidate_quote: str = Field(default="", max_length=3000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=5, max_length=500)


class Score(EvalModel):
    score: StrictInt | None = Field(ge=1, le=5)
    reason: str = Field(min_length=5, max_length=500)


class Scores(EvalModel):
    coverage: Score
    banking_correctness: Score
    evidence_reasoning: Score
    actionability: Score
    clarity: Score


class Verdict(EvalModel):
    findings: list[Finding] = Field(min_length=4, max_length=8)
    scores: Scores
    failure_modes: list[Literal["no_answer", "wrong_intent", "lost_context", "wrong_financial_fact",
        "unsupported_claim", "over_refusal", "dead_end", "unnecessary_clarification", "jargon",
        "stale_source", "unauthorized_action", "other"]] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
    summary: str = Field(min_length=5, max_length=500)

    def validate_against(self, packet, revision=None):
        wanted = {r["id"] for r in packet.requirements}
        actual = [f.criterion_id for f in self.findings]
        if len(actual) != len(set(actual)) or set(actual) != wanted:
            raise ValueError("criterion_id must cover exactly "+str(sorted(wanted))+"; received "+str(actual))
        references = {e.id for e in packet.evidence}
        for f in self.findings:
            if set(f.evidence_ids) - references:
                raise ValueError("Invented source reference")
            if f.verdict == "met" and not f.candidate_quote.strip():
                raise ValueError("A met finding needs an exact candidate quote")
            if f.candidate_quote and f.candidate_quote not in packet.candidate_output:
                raise ValueError("candidate_quote for "+f.criterion_id+" is not an exact candidate substring; copy a shorter original span without punctuation changes")
        required = {r["id"] for r in packet.requirements if r["required"]}
        if any(f.criterion_id in required and f.verdict == "not_met" for f in self.findings):
            if self.scores.coverage.score is not None and self.scores.coverage.score > 3:
                raise ValueError("Coverage contradicts a failed mandatory requirement")
        for name in rubric.DIMENSIONS:
            applicable = name in packet.applicable_dimensions
            if (getattr(self.scores, name).score is not None) != applicable:
                raise ValueError("An applicable dimension is omitted or an inapplicable dimension was given points")
        if "wrong_financial_fact" in self.failure_modes and (self.scores.banking_correctness.score or 0) > 2:
            raise ValueError("Banking score contradicts a material financial error")
        if "unauthorized_action" in self.failure_modes and (self.scores.banking_correctness.score or 0) > 1:
            raise ValueError("Unauthorized action cannot receive a positive banking score")
        if revision in {'v10','v11','v12'}:
            required_rows=[r for r in packet.requirements if r['required']]
            met={f.criterion_id for f in self.findings if f.verdict=='met'}
            total=sum(r['weight'] for r in required_rows)
            credit=sum(r['weight'] for r in required_rows if r['id'] in met)
            cap=5 if credit==total else 3 if credit*4>=total*3 else 2 if credit*2>=total else 1
            if (self.scores.coverage.score or 0)>cap:
                raise ValueError(f'V10 coverage exceeds mandatory completion cap {cap}; not_met and insufficient_evidence are not met')
            known=set(candidate_quote_spans(packet))
            for name in packet.applicable_dimensions:
                item=getattr(self.scores,name)
                if item.score>=4 and not (set(re.findall(r'Q\d{3}',item.reason)) & known):
                    raise ValueError(f'V10 high {name} score requires an actual Q-number citation in its reason')
        if revision in {'v11','v12'}:
            check_explicit_shortfalls(self,packet)
        return self


def check_explicit_shortfalls(verdict,packet):
    """The evaluator may not waive an explicitly required monetary deficit.

    This derives predicates from the frozen requirement, with no case IDs or
    expected amounts embedded in application/evaluator code.
    """
    amount=r'(\d+(?:\.\d+)?)\s*(万?元)'
    labels=r'(?:缺口|短缺|不足|差额|相差|还差|还少|少|缺|差)'
    def normalize(text):
        return re.sub(r'(?<=\d)[,，](?=\d{3}(?:\D|$))','',text).replace('＝','=')
    shown=normalize(packet.candidate_output)
    claims=[]
    display_amount=r'(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>万?元)?'
    for match in re.finditer(labels+r'\s*(?:为|是|还有|约|共计|[:：])?\s*[¥￥]?\s*'+display_amount,shown):
        if re.search(r'不是|并非|并不|不需要',shown[max(0,match.start()-5):match.start()]):
            continue
        claims.append(Decimal(match['number'])*(10000 if match['unit']=='万元' else 1))
    findings={f.criterion_id:f for f in verdict.findings}
    for requirement in packet.requirements:
        text=normalize(requirement['text'])
        if (not requirement['required'] or findings[requirement['id']].verdict!='met'
                or not re.search(r'给出|报告|说明|指出|明确|展示',text)):
            continue
        for match in re.finditer(labels+r'\s*'+amount,text):
            before=text[max(0,match.start()-12):match.start()]
            if re.search(r'不能声称|不要声称|不可声称|不必|无需',before):
                continue
            wanted=Decimal(match[1])*(10000 if match[2]=='万元' else 1)
            if wanted not in claims:
                raise ValueError('V11 met finding lacks its explicitly required deficit amount: '+requirement['id'])


_FAILURE_MODES = ["no_answer", "wrong_intent", "lost_context", "wrong_financial_fact",
    "unsupported_claim", "over_refusal", "dead_end", "unnecessary_clarification", "jargon",
    "stale_source", "unauthorized_action", "other"]


def _candidate_quote_slices(packet, max_spans=128, max_chars=2000):
    """Build bounded, exact contiguous excerpts; skip standalone evaluator headers."""
    lines, offset = [], 0
    for line in packet.candidate_output.splitlines(keepends=True):
        value = line.strip()
        if value and not (value.startswith("[") and value.endswith("]")):
            leading = len(line)-len(line.lstrip())
            lines.append((offset+leading, offset+leading+len(value)))
        offset += len(line)
    if not lines:
        return []
    group_size = max(1, (len(lines)+max_spans-1)//max_spans)
    slices = []

    def add_group(group):
        start, end = group[0][0], group[-1][1]
        if end-start <= max_chars:
            slices.append((start, end))
        elif len(group) > 1:
            middle = len(group)//2
            add_group(group[:middle])
            add_group(group[middle:])
        else:
            for part_start in range(start, end, max_chars):
                slices.append((part_start, min(end, part_start+max_chars)))

    for start in range(0, len(lines), group_size):
        add_group(lines[start:start+group_size])
    if len(slices) > max_spans:
        raise ValueError("JUDGE_TOO_MANY_CITATION_SPANS")
    return slices


def candidate_quote_spans(packet):
    """Map short quote IDs to exact candidate substrings for model selection."""
    return {f"Q{index:03d}": packet.candidate_output[start:end]
        for index, (start, end) in enumerate(_candidate_quote_slices(packet), 1)}


def marked_candidate_output(packet, spans=None):
    """Add small evaluator citation IDs without changing candidate wording."""
    slices = _candidate_quote_slices(packet)
    output = packet.candidate_output
    for index, (start, _end) in reversed(list(enumerate(slices, 1))):
        output = output[:start]+f"⟦Q{index:03d}⟧"+output[start:]
    return output


def strict_verdict_tool(packet, revision="v7"):
    """MiMo strict function schema; v6 keeps its original map for reproducibility."""
    quote_ids = ["", *candidate_quote_spans(packet).keys()]
    source_ids = [item.id for item in packet.evidence]
    finding_schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["met", "not_met", "insufficient_evidence"]},
            "candidate_quote_id": {"type": "string", "enum": quote_ids},
            "evidence_ids": {"type": "array", "items": {"type": "string", "enum": source_ids}},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "candidate_quote_id", "evidence_ids", "reason"],
        "additionalProperties": False,
    }
    dimensions = ["coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity"]
    score_schema = {"type": "object", "properties": {
        "score": {"type": "integer", "enum": None},
        "reason": {"type": "string"},
    }, "required": ["score", "reason"], "additionalProperties": False}
    score_schemas = {}
    for dimension in dimensions:
        if revision in {"v7", "v8", "v9", "v10", "v11", "v12"}:
            score_schema["properties"]["score"]["enum"] = list(range(1, 6)) if dimension in packet.applicable_dimensions else [0]
        elif revision == "v6":
            score_schema["properties"]["score"]["enum"] = [0, 1, 2, 3, 4, 5]
        else:
            raise ValueError("JUDGE_STRICT_SCHEMA_REVISION_UNKNOWN")
        # Each dimension gets its own copy so its valid range is task-specific.
        score_schemas[dimension] = json.loads(json.dumps(score_schema))
    ids = [item["id"] for item in packet.requirements]
    if revision == "v6":
        finding_definition = {"type": "object", "properties": {key: finding_schema for key in ids},
            "required": ids, "additionalProperties": False}
    else:
        finding_definition = {"type": "array", "items": {"type": "object", "properties": {
            "criterion_id": {"type": "string", "enum": ids}, **finding_schema["properties"]},
            "required": ["criterion_id", *finding_schema["required"]], "additionalProperties": False}}
    schema = {"type": "object", "properties": {
        "findings": finding_definition,
        "scores": {"type": "object", "properties": score_schemas,
            "required": dimensions, "additionalProperties": False},
        "failure_modes": {"type": "array", "items": {"type": "string", "enum": _FAILURE_MODES}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "summary": {"type": "string"},
    }, "required": ["findings", "scores", "failure_modes", "confidence", "summary"],
        "additionalProperties": False}
    tool = {"type": "function", "function": {"name": "submit_verdict",
        "description": "提交本题评分；必须按给定字段和枚举输出，不执行任何银行操作。",
        "strict": True, "parameters": schema}}
    validate_strict_tool(tool)
    return tool


def validate_strict_tool(tool):
    """Reject schema gaps before sending a packet to the provider."""
    function = tool.get("function", {})
    if tool.get("type") != "function" or function.get("strict") is not True:
        raise ValueError("JUDGE_STRICT_TOOL_DISABLED")
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum"}

    def visit(node):
        if not isinstance(node, dict) or set(node)-allowed:
            raise ValueError("JUDGE_STRICT_SCHEMA_UNSUPPORTED_KEY")
        if node.get("type") == "object":
            props = node.get("properties")
            if (not isinstance(props, dict) or node.get("additionalProperties") is not False or
                    set(node.get("required", [])) != set(props)):
                raise ValueError("JUDGE_STRICT_OBJECT_FIELDS_NOT_REQUIRED")
            for child in props.values():
                visit(child)
        elif node.get("type") == "array":
            if "items" not in node:
                raise ValueError("JUDGE_STRICT_ARRAY_ITEMS_MISSING")
            visit(node["items"])
        elif node.get("type") not in {"string", "integer"}:
            raise ValueError("JUDGE_STRICT_SCHEMA_TYPE_UNSUPPORTED")
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            raise ValueError("JUDGE_STRICT_ENUM_INVALID")

    visit(function.get("parameters"))
    return True


def normalize_tool_verdict(arguments, packet, warnings=None):
    """Validate strict tool arguments, map quote IDs to exact source spans, then apply business checks."""
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        raise ValueError("tool_arguments_not_object")
    expected_ids = [item["id"] for item in packet.requirements]
    expected_top = {"findings", "scores", "failure_modes", "confidence", "summary"}
    if not expected_top <= set(arguments):
        raise ValueError("tool_top_level_fields_mismatch")
    extras = set(arguments)-expected_top
    if extras and warnings is not None:
        warnings.append({"path": "top_level", "ignored_fields": sorted(_safe_field_name(x) for x in extras)})
    arguments = {key: arguments[key] for key in expected_top}
    findings_input = arguments["findings"]
    if isinstance(findings_input, str):
        findings_input, parse_mode = _decode_json_field(findings_input)
        if findings_input is None:
            raise ValueError("tool_findings_not_json") from None
        if warnings is not None and parse_mode != "json":
            warnings.append({"path": "findings", "normalized_from": parse_mode})
    if isinstance(findings_input, list):
        findings_by_id = {}
        for entry in findings_input:
            if not isinstance(entry, dict) or not isinstance(entry.get("criterion_id"), str):
                raise ValueError("tool_finding_item_invalid")
            criterion_id = entry["criterion_id"]
            if criterion_id in findings_by_id:
                raise ValueError("tool_finding_id_duplicate")
            fields = {"verdict", "candidate_quote_id", "evidence_ids", "reason"}
            if not fields <= set(entry):
                raise ValueError("tool_finding_fields_missing")
            extra_fields = set(entry)-fields-{"criterion_id"}
            if extra_fields and warnings is not None:
                warnings.append({"path": "finding."+_safe_field_name(criterion_id),
                    "ignored_fields": sorted(_safe_field_name(x) for x in extra_fields)})
            findings_by_id[criterion_id] = {k: entry[k] for k in fields}
    elif isinstance(findings_input, dict):
        findings_by_id = findings_input
    else:
        raise ValueError("tool_findings_type_invalid")
    if set(findings_by_id) != set(expected_ids):
        raise ValueError("tool_finding_ids_mismatch")
    spans = candidate_quote_spans(packet)
    findings = []
    for criterion_id in expected_ids:
        item = findings_by_id[criterion_id]
        fields = {"verdict", "candidate_quote_id", "evidence_ids", "reason"}
        if not isinstance(item, dict) or not fields <= set(item):
            raise ValueError("tool_finding_fields_mismatch")
        extra_fields = set(item)-fields
        if extra_fields and warnings is not None:
            warnings.append({"path": "finding."+_safe_field_name(criterion_id),
                "ignored_fields": sorted(_safe_field_name(x) for x in extra_fields)})
        quote_id = item.pop("candidate_quote_id")
        if quote_id not in {"", *spans}:
            raise ValueError("tool_quote_id_unknown")
        findings.append({"criterion_id": criterion_id, "candidate_quote": spans.get(quote_id, ""), **item})
    scores = arguments["scores"]
    if isinstance(scores, str):
        scores, parse_mode = _decode_json_field(scores)
        if scores is None:
            raise ValueError("tool_scores_not_json") from None
        if warnings is not None and parse_mode != "json":
            warnings.append({"path": "scores", "normalized_from": parse_mode})
    dimensions = ["coverage", "banking_correctness", "evidence_reasoning", "actionability", "clarity"]
    if not isinstance(scores, dict) or not set(dimensions) <= set(scores):
        raise ValueError("tool_score_dimensions_mismatch")
    extra_dimensions = set(scores)-set(dimensions)
    if extra_dimensions and warnings is not None:
        warnings.append({"path": "scores", "ignored_fields": sorted(_safe_field_name(x) for x in extra_dimensions)})
    normalized_scores = {}
    for dimension in dimensions:
        score = scores[dimension]
        if not isinstance(score, dict) or not {"score", "reason"} <= set(score):
            raise ValueError("tool_score_fields_mismatch")
        extra_fields = set(score)-{"score", "reason"}
        if extra_fields and warnings is not None:
            warnings.append({"path": "scores."+dimension, "ignored_fields": sorted(_safe_field_name(x) for x in extra_fields)})
        if type(score["score"]) is not int or score["score"] not in {0, 1, 2, 3, 4, 5}:
            raise ValueError("tool_score_value_invalid")
        normalized_scores[dimension] = {"score": None if score["score"] == 0 else score["score"], "reason": score["reason"]}
    failure_modes = arguments["failure_modes"]
    if isinstance(failure_modes, str):
        failure_modes, parse_mode = _decode_json_field(failure_modes)
        if failure_modes is None:
            raise ValueError("tool_failure_modes_not_json") from None
        if warnings is not None and parse_mode != "json":
            warnings.append({"path": "failure_modes", "normalized_from": parse_mode})
    normalized = {**arguments, "findings": findings, "scores": normalized_scores, "failure_modes": failure_modes}
    return Verdict.model_validate(normalized).validate_against(packet)


def _decode_json_field(value):
    """Decode a JSON-valued string only; permit fences/preambles, never infer missing data."""
    try:
        return json.loads(value), "json"
    except (ValueError, TypeError):
        pass
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", value.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned), "fenced_json"
    except (ValueError, TypeError):
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            parsed, _end = decoder.raw_decode(cleaned, index)
            if isinstance(parsed, (dict, list)):
                return parsed, "embedded_json"
        except ValueError:
            continue
    return None, "unparseable_string"


def _safe_field_name(value):
    value = str(value)
    return value if value.isidentifier() and len(value) <= 64 else "<other_field>"


def tool_argument_shape(arguments):
    """Summarize field names and types only; never retain model-generated values."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            return {"root_type": "invalid_json_string"}
    if not isinstance(arguments, dict):
        return {"root_type": type(arguments).__name__}

    def keys(value):
        if isinstance(value, str):
            _parsed, kind = _decode_json_field(value)
            return {"type": "string", "length": len(value), "encoding": kind}
        if not isinstance(value, dict):
            return {"type": type(value).__name__}
        return {"type": "object", "keys": sorted(
            str(key) if isinstance(key, str) and key.isidentifier() and len(key) <= 64 else "<other_key>"
            for key in value)}

    result = {"root_type": "object", "root_keys": keys(arguments)["keys"]}
    for key in ("findings", "scores"):
        if key in arguments:
            result[key] = keys(arguments[key])
            if isinstance(arguments[key], dict) and arguments[key]:
                first = next(iter(arguments[key].values()))
                result[key]["first_value"] = keys(first)
            elif isinstance(arguments[key], list) and arguments[key]:
                result[key]["first_item"] = keys(arguments[key][0])
    return result


def system_prompt(packet=None, revision=None):
    if revision == 'v12':
        from .rubric_v12 import PROMPT
        return PROMPT if packet is None else PROMPT+'\n本题requirements ID：'+json.dumps([r['id'] for r in packet.requirements])+'；可引用编号：'+json.dumps(list(candidate_quote_spans(packet)))
    if revision == 'v11':
        from .rubric_v11 import PROMPT
        return PROMPT if packet is None else PROMPT+'\n本题requirements ID：'+json.dumps([r['id'] for r in packet.requirements])+'；可引用编号：'+json.dumps(list(candidate_quote_spans(packet)))
    if revision == 'v10':
        from .rubric_v10 import PROMPT
        return PROMPT if packet is None else PROMPT+'\n本题requirements ID：'+json.dumps([r['id'] for r in packet.requirements])+'；可引用编号：'+json.dumps(list(candidate_quote_spans(packet)))
    if revision == "v9":
        from .rubric_v9 import PROMPT
        return PROMPT if packet is None else PROMPT + '\n本题requirements ID：' + json.dumps([r['id'] for r in packet.requirements]) + '；可引用编号：' + json.dumps(list(candidate_quote_spans(packet)))
    if revision == "v8":
        from .rubric_v8 import PROMPT
        if packet is None:
            return PROMPT
        return PROMPT + "\n本题requirements ID：" + json.dumps([r["id"] for r in packet.requirements], ensure_ascii=False) + \
            "；applicable_dimensions：" + json.dumps(packet.applicable_dimensions, ensure_ascii=False) + \
            "；可引用编号：" + json.dumps(list(candidate_quote_spans(packet)), ensure_ascii=False) + "。"
    if revision == "v7":
        from .rubric_v7 import PROMPT
        if packet is None:
            return PROMPT
        return PROMPT + "\n本题requirements ID：" + json.dumps([r["id"] for r in packet.requirements], ensure_ascii=False) + \
            "；applicable_dimensions：" + json.dumps(packet.applicable_dimensions, ensure_ascii=False) + \
            "；可引用编号：" + json.dumps(list(candidate_quote_spans(packet)), ensure_ascii=False) + "。"
    if revision == "v6":
        from .rubric_v6 import PROMPT
        if packet is None:
            return PROMPT
        return PROMPT + "\n本题要求ID：" + json.dumps([r["id"] for r in packet.requirements], ensure_ascii=False) + \
            "；适用评分维度：" + json.dumps(packet.applicable_dimensions, ensure_ascii=False) + \
            "；可引用编号：" + json.dumps(list(candidate_quote_spans(packet)), ensure_ascii=False) + "。"
    if revision == "v6":
        from .rubric_v6 import PROMPT
        if packet is None:
            return PROMPT
        return PROMPT + "\n本题要求ID：" + json.dumps([r["id"] for r in packet.requirements], ensure_ascii=False) + \
            "；适用评分维度：" + json.dumps(packet.applicable_dimensions, ensure_ascii=False) + \
            "；可引用编号：" + json.dumps(list(candidate_quote_spans(packet)), ensure_ascii=False) + "。"
    if revision == "v5":
        from .rubric_v5 import PROMPT
        ids = [r["id"] for r in packet.requirements] if packet is not None else []
        dimensions = packet.applicable_dimensions if packet is not None else list(rubric.DIMENSIONS)
        task_contract = (
            "\n本题criterion_id必须且只可使用：" + json.dumps(ids, ensure_ascii=False) +
            "。applicable_dimensions为：" + json.dumps(dimensions, ensure_ascii=False) +
            "。" if packet is not None else "\n按本题requirements中的ID逐项输出；适用维度以applicable_dimensions为准。"
        )
        return PROMPT + task_contract
    schema = Verdict.model_json_schema()
    notice = ""
    if packet is not None:
        ids = [r["id"] for r in packet.requirements]
        schema["$defs"]["Finding"]["properties"]["criterion_id"]["enum"] = ids
        schema["properties"]["findings"].update(minItems=len(ids), maxItems=len(ids))
        notice = "\n本题必须逐项覆盖这些criterion_id，不能改名："+json.dumps(ids)+"。"
    v4 = revision == "v4" or (packet and packet.observation_scope == "browser_dom_regions_v3")
    if v4 or revision == "v3" or (packet and packet.observation_scope == "browser_dom_regions_v2"):
        from .rubric_v3 import PROMPT
        prompt = PROMPT
    else:
        prompt = rubric.PROMPT
    if v4:
        definitions = schema.get("$defs", {})
        def inline(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    return inline(definitions[value["$ref"].split("/")[-1]])
                return {k:inline(v) for k,v in value.items() if k not in {"$defs","title"}}
            return [inline(v) for v in value] if isinstance(value,list) else value
        schema = inline(schema)
        dims = packet.applicable_dimensions if packet else list(rubric.DIMENSIONS)
        example = {"findings":[{"criterion_id": packet.requirements[0]["id"] if packet else "r1",
            "verdict":"met","candidate_quote":"这里必须替换为候选原句","evidence_ids":[],"reason":"这里只演示字段层级，不是本题结论"}],
            "scores":{d:{"score":3 if d in dims else None,"reason":"这里只演示字段类型，请按实际证据评分"} for d in rubric.DIMENSIONS},
            "failure_modes":[],"confidence":"medium","summary":"结构示例，不代表本题结果"}
        prompt += ("\n【输出层级示例】这只是格式示例，分值、finding数量和判定均须根据本题重新填写，不能照抄。"
            "scores的五个维度每个都是含score与reason的对象，即使不适用也用{score:null,reason:说明}；不能直接写null。"
            "所有维度都在scores内部，confidence和summary在顶层。"+json.dumps(example,ensure_ascii=False))
    return prompt+notice+"\nJSON Schema："+json.dumps(schema, ensure_ascii=False)


def content_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ServiceJudge(MiMoJudge):
    """Reuse MiMo credentials, rate scheduling and semaphore, not its legacy rubric."""

    def __init__(self, settings, transport=None):
        super().__init__(settings,transport)
        self._halted_code = None
        self.revision = None

    async def grade(self, packet: Packet):
        if not self.settings.api_key:
            raise JudgeError("JUDGE_NOT_CONFIGURED")
        if self.revision == "v6" and self.settings.thinking:
            raise JudgeError("JUDGE_V6_REQUIRES_THINKING_DISABLED")
        if self.revision == "v8" and self.settings.format_retries != 2:
            raise JudgeError("JUDGE_V8_REQUIRES_TWO_FORMAT_RETRIES")
        # Redaction must precede schema validation and quote validation.
        packet = Packet.model_validate(scrub(packet.model_dump(mode="json")))
        system, attempts, notice = system_prompt(packet, revision=self.revision), [], ""
        strict_mode = self.revision in {"v6", "v7", "v8", "v9", "v10", "v11", "v12"}
        if strict_mode:
            body_packet = packet.model_dump(mode="json")
            spans = candidate_quote_spans(packet)
            body_packet["candidate_output"] = marked_candidate_output(packet, spans)
            body_text = json.dumps(body_packet, ensure_ascii=False, sort_keys=True)
            tool = strict_verdict_tool(packet, self.revision)
            tool_hash = content_hash(tool)
        else:
            body_text = json.dumps(packet.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            tool, tool_hash = None, None
        format_retry = transient_retries = timeout_retries = request_number = 0
        async with self._semaphore:
            if self._halted_code:
                raise JudgeError("JUDGE_SCHEDULER_STOPPED_"+self._halted_code)
            async with httpx.AsyncClient(timeout=self.settings.timeout, trust_env=False,
                    follow_redirects=False, transport=self.transport) as client:
                while True:
                    await self._rate_slot()
                    if self._halted_code:
                        raise JudgeError("JUDGE_SCHEDULER_STOPPED_"+self._halted_code, attempts)
                    body = {"model": self.settings.model, "messages": [
                        {"role": "system", "content": system+notice}, {"role": "user", "content": body_text}],
                        "stream": False,
                        "thinking": {"type": "enabled" if self.settings.thinking else "disabled"},
                        "max_completion_tokens": self.settings.max_completion_tokens}
                    if strict_mode:
                        body["tools"] = [tool]
                        # MiMo documents `auto` as its supported tool_choice mode;
                        # a single declared tool plus an explicit system contract selects it.
                        body["tool_choice"] = "auto"
                    else:
                        body["response_format"] = {"type": "json_object"}
                    started = time.perf_counter()
                    request_number += 1
                    meta = {"attempt": request_number, "usage": None}
                    output_shape = None
                    try:
                        async with asyncio.timeout(self.settings.timeout + 35):
                            response = await client.post(self.settings.base_url+"/chat/completions", json=body,
                                headers={"api-key": self.settings.api_key, "Content-Type": "application/json"})
                    except (TimeoutError, httpx.TimeoutException):
                        meta.update(status="deadline_exceeded", duration_ms=round((time.perf_counter()-started)*1000, 2))
                        attempts.append(meta)
                        if timeout_retries < 1:
                            timeout_retries += 1
                            await asyncio.sleep(3)
                            continue
                        raise JudgeError("JUDGE_DEADLINE_EXCEEDED", attempts) from None
                    except httpx.HTTPError:
                        meta.update(status="transport_error", duration_ms=round((time.perf_counter()-started)*1000, 2))
                        attempts.append(meta)
                        if transient_retries < 2:
                            delay = 2 ** (transient_retries+1)
                            transient_retries += 1
                            await asyncio.sleep(delay)
                            continue
                        raise JudgeError("JUDGE_TRANSPORT_ERROR", attempts) from None
                    meta.update(http_status=response.status_code, duration_ms=round((time.perf_counter()-started)*1000, 2))
                    if response.status_code != 200:
                        if response.status_code in {401,402,403}:
                            self._halted_code = f"JUDGE_HTTP_{response.status_code}"
                        meta["status"] = "http_error"
                        attempts.append(meta)
                        if response.status_code in {408,425,429} or response.status_code >= 500:
                            if transient_retries < 2:
                                retry_after = response.headers.get("Retry-After", "")
                                try:
                                    delay = min(30, max(1, int(retry_after))) if retry_after else 2 ** (transient_retries+1)
                                except ValueError:
                                    delay = 2 ** (transient_retries+1)
                                transient_retries += 1
                                if response.status_code == 429:
                                    async with self._rate_lock:
                                        self._next_start = max(self._next_start, time.monotonic()+delay)
                                await asyncio.sleep(delay)
                                continue
                        raise JudgeError(f"JUDGE_HTTP_{response.status_code}", attempts)
                    try:
                        data = response.json()
                        meta.update(provider_model=data.get("model"), usage=scrub(data.get("usage")))
                        choice = data["choices"][0]
                        message = choice["message"]
                        if strict_mode:
                            calls = message.get("tool_calls") or []
                            if choice.get("finish_reason") != "tool_calls" or len(calls) != 1:
                                raise ValueError("expected_one_tool_call")
                            function = calls[0].get("function") or {}
                            if function.get("name") != "submit_verdict":
                                raise ValueError("unexpected_tool_name")
                            arguments = function.get("arguments")
                            output_shape = tool_argument_shape(arguments)
                            output_warnings = []
                            verdict = normalize_tool_verdict(arguments, packet, output_warnings)
                            verdict.validate_against(packet,self.revision)
                            if output_warnings:
                                meta["ignored_extra_fields"] = output_warnings[:12]
                        else:
                            if choice.get("finish_reason") != "stop" or message.get("tool_calls"):
                                raise ValueError("incomplete_or_unexpected_judge_response")
                            verdict = Verdict.model_validate_json(message["content"]).validate_against(packet)
                    except (ValueError, TypeError, KeyError, IndexError) as exc:
                        meta["status"] = "format_error"
                        if strict_mode and output_shape is not None:
                            meta["output_shape"] = output_shape
                        if isinstance(exc, ValidationError):
                            # Field paths/types only; never save invalid raw text,
                            # provider private reasoning or Pydantic input values.
                            meta["validation_error"] = json.dumps([
                                {"loc": e["loc"], "type": e["type"]}
                                for e in exc.errors(include_input=False, include_context=False, include_url=False)[:8]])
                        else:
                            meta["validation_error"] = str(exc)[:250] if type(exc) is ValueError else type(exc).__name__
                        attempts.append(meta)
                        if format_retry < self.settings.format_retries:
                            format_retry += 1
                            if self.revision in {"v6", "v7", "v8", "v9", "v10", "v11", "v12"}:
                                notice = ("\n上次结构校验失败："+meta["validation_error"]+
                                    "。按当前工具参数重新核对全部字段，只改正格式/引用错误，分数仍依据原始材料。")
                            elif self.revision == "v5":
                                notice = ("\n上次校验错误："+meta["validation_error"]+
                                    "。重新核对后只修正该错误。顶层五键为findings,scores,failure_modes,confidence,summary；"
                                    "scores仅含五个评分维度。finding ID须与题目完全一致；必需项未满足时coverage≤3；"
                                    "wrong_financial_fact时banking_correctness≤2，unauthorized_action时≤1。"
                                    "不要改变有证据支持的判定。")
                            else:
                                notice = ("\n上次结构或引用校验未通过："+meta["validation_error"]+
                                    "。重新独立核对原题；引用必须逐字来自candidate_output，适用维度按applicable_dimensions，必需项失败时coverage不超过3。")
                            continue
                        raise JudgeError("JUDGE_OUTPUT_INVALID", attempts) from None
                    meta["status"] = "ok"
                    attempts.append(meta)
                    from .rubric_v3 import VERSION as V3
                    if self.revision == 'v12':
                        from .rubric_v12 import VERSION as version
                    elif self.revision == 'v11':
                        from .rubric_v11 import VERSION as version
                    elif self.revision == 'v10':
                        from .rubric_v10 import VERSION as version
                    elif self.revision == "v9":
                        from .rubric_v9 import VERSION as version
                    elif self.revision == "v8":
                        from .rubric_v8 import VERSION as version
                    elif self.revision == "v7":
                        from .rubric_v7 import VERSION as version
                    elif self.revision == "v6":
                        from .rubric_v6 import VERSION as version
                    elif self.revision == "v5":
                        from .rubric_v5 import VERSION as V5
                        version = V5
                    else:
                        version = "finservice-atomic-judge-v4-schema" if packet.observation_scope == "browser_dom_regions_v3" else V3 if packet.observation_scope == "browser_dom_regions_v2" else rubric.VERSION
                    return {"status": "ok", "verdict": verdict.model_dump(mode="json"), "attempts": attempts,
                        "rubric_version": version, "packet_hash": content_hash(packet.model_dump(mode="json")),
                        "prompt_hash": content_hash(system), "tool_schema_hash": tool_hash}
