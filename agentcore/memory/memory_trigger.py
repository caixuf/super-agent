#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆写入触发：从一轮对话里判断「是否形成了新的事实/决策」，命中才写。

为什么需要这一层（2026-10-23 实测）：
    库里有 45 条事实，全部 source=<某个业务来源>，锚点写入链路 <某个业务工具> 的
    source=decision_anchor 一条都没有 —— 工具从来没人调用过。根因是
    「工具存在，但没有行为约定」：SOUL.md 里没有一句说什么时候该存，
    模型自然想不起来。同理 HippocampusEngine 的库从未被创建过。

    Mem0 的做法是 memory.add() 每次都过一遍抽取，写入是主动的、自动的。
    本模块补的就是这一环：不依赖 agent 自觉，在对话结束后由平台侧判定。

设计取舍：
    1. 先做纯规则（零成本、可解释），命中不了再交给 LLM 复核。
       理由：45 条规模上，规则足以覆盖「定了/改成/结论是」这类明确信号；
       上 LLM 每轮都要花一次调用，而绝大多数闲聊轮次没有任何可存的事实。
    2. 规则只负责筛掉明显不该存的，不负责判断对错。宁可多给 LLM
       一次复核，也不要让规则把真正的决策漏掉（漏存比多存更难发现）。
    3. 抽取失败一律返回 skip + reason，绝不阻断对话。
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("trigger"))

# 只收录明确的结论性表达与个人/人设事实。
# 涵盖两类：1. 技术决策与踩坑；2. 个人身份、习惯偏好、交互风格约定。
DECISION_PATTERNS = (
    # 技术结论与规范
    r"(决定|定了|已定|拍板|就这么|采用|改用|改成|换成|确定用|已经确定|已确定|已经确认|已确认|我同意)",
    r"(结论是|根因是|原因是|本质上是|实际上是因为)",
    r"(不要|禁止|必须|一律|绝不能|永远不要)",
    r"(踩过|踩坑|踩了|教训是|之前遇到|曾经因为)",
    r"(以后|下次|后续)(都|要|不要|必须|记得)",
    r"(?:先|首先|优先).{1,80}(?:再|之后|然后)",
    # 个人事实、称呼、习惯偏好与人设约定（让机器人具备性格）
    r"(我叫|我是|我的名字|本人|自称|别名|外号|昵称|花名)",
    r"(叫我|称呼我|别叫我|把我当)",
    r"(我喜欢|我习惯|我偏好|我不喜欢|我讨厌|反感)",
    r"(回答我时|说话风格|语气|你以后|作为助手|你的角色|人设)",
    r"(?:文档|回答|回复|解释|说明).{0,20}(?:讲人话|可读性|简洁|清楚|详细)",
    r"(?:以后|今后|默认|每次|所有).{0,16}(?:回答|回复|文档|沟通|称呼|工具)",
)

# 反问/试探/闲聊：即便含有决策词，也基本不是结论
# 注意「吧」不能单独当反问词："定了改 5% 了吧" 是确认，不是提问。
# 只有明确疑问词或真正的问号结尾才拦。
NEGATIVE_PATTERNS = (
    r"(?:^|[。！!\n])\s*(?:为什么|怎么|如何|是否|能不能|可不可以|有没有)",
    r"(?:^|[。！!\n])[^。！!\n]{0,120}[?？]\s*(?:$|[。！!\n])",
    r"^(你好|谢谢|收到|好的|OK|ok)\s*[!！。]?$",
    r"(吗|呢)\s*[?？]?$",
)

MIN_QUERY_CHARS = 4
MAX_INPUT_CHARS = 6000


def should_consider(user_message: str, assistant_reply: str = "") -> Dict[str, Any]:
    """规则预筛：这一轮值不值得走抽取。"""
    q = (user_message or "").strip()
    a = (assistant_reply or "").strip()
    if len(q) < MIN_QUERY_CHARS:
        return {"consider": False, "reason": "query_too_short", "matched": []}

    negative = any(re.search(pat, q) for pat in NEGATIVE_PATTERNS)
    matched: List[str] = []
    for pat in DECISION_PATTERNS:
        for m in re.finditer(pat, q + " " + a):
            matched.append(m.group(0))
    if not matched:
        return {"consider": False,
                "reason": "question_or_chitchat" if negative else "no_decision_signal",
                "matched": []}
    # A pure question/chitchat must not reach extraction. Mixed turns do: the
    # candidate review checks the quoted sentence, so "是否改用英文？以后默认
    # 中文回复。" can preserve the independent decision while rejecting the
    # question. Additive cue patterns ("以后") do not prove confirmation.
    pure_negative = (
        re.search(r"(吗|呢)\s*[?？]?\s*$", q)
        or re.search(r"^(你好|谢谢|收到|好的|OK|ok)\s*[!！。]?$", q, re.IGNORECASE)
        or (bool(negative) and not bool(re.search(
            r"(决定|定了|已定|拍板|采用|改用|改成|换成|确定用|已确认|"
            r"我喜欢|我习惯|我偏好|我不喜欢|我讨厌|"
            r"(?:以后|默认|每次|所有).{0,16}(?:回答|回复|文档|沟通))", q)))
    )
    if pure_negative:
        return {"consider": False, "reason": "question_or_chitchat", "matched": []}
    return {"consider": True, "reason": "decision_signal", "matched": matched[:5]}


EXTRACT_SYSTEM = """你是一个智能体记忆系统的全自动写入抽取器。从用户与助手的对话里提炼长期有价值的原子事实。

提炼范畴分为两类：
1. 【技术经验 (technical)】：
   - 已确定的方案/决策/参数门限（定了/改成了…）
   - 踩坑与失败根因（之前因为…失败/根因是…）
   - 自动化测试与工程约束（必须/禁止/绝不能）
2. 【人物画像与人设偏好 (personal)】：
   - 用户的身份、称呼、别名（例如：用户真名叫李龙飞，自称/昵称菜虚飞）
   - 用户的沟通习惯与偏好（例如：喜欢简洁、讨厌废话、排查问题先给结论）
   - 机器人的人设与调性约定（例如：助手与该用户沟通应保持干练专业的口吻）

绝对不要提炼：
- 提问本身（如“怎么配置门限？”）
- 礼貌寒暄（如“好的谢谢”）
- 临时过程态（如“正在跑测试”、“请稍等”）
- 未定论的讨论或猜测
- 仅约束本轮的命令、工具限制或输出要求，例如“这次不要改文件”“请直接回答，不调用工具”。
  不能把它们改写成长期偏好。只有用户在引文中明确约定长期、以后、默认或每次适用的规则才可保存。

输出严格 JSON（不要任何 markdown 代码块包裹，直接输出 JSON 字符串）：
{
  "facts": [
    {
      "subject": "简短主题，20字以内",
      "statement": "自成体系的一句话事实，脱离上下文也能完全读懂",
      "ebm": "指向代际时填具体代际标识，个人习惯与通用事实务必留空",
      "kind": "technical 或 personal 或 context",
      "tags": {"domain": "...", "confidence": "high"}
    }
  ]
}
若无任何值得长期记住的结论，输出 {"facts": []}。
"""



def _truncate(text: str, limit: int = MAX_INPUT_CHARS) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "…(已截断)"


def _load_chat_completions():
    """取当前可用的对话补全函数，取不到返回 None（调用方降级跳过本环节）。

    引擎不自行导入任何 LLM 客户端：那是**宿主的依赖**，由 MemoryConfig
    通过 `llm_fn` 或 `llm_loader` 注入。写死包名会让这个库绑死在某一家的
    目录布局上（历史上正是靠"补 sys.path 再导入"才勉强可用，一旦加载失败
    被当成"本轮跳过"，表现为记忆库长期为空 —— 规则层已命中但抽取从未成功）。
    """
    return get_config().resolve_llm()


class ExtractionError(ValueError):
    """Public bounded code; never includes conversation or provider credentials."""


def turn_scoped_constraint(quote):
    """Do not promote a per-request capability restriction into a lasting rule."""
    constraint=re.search(r'(?:不(?:要|用|必|需)?|无需|禁止|别|只|仅).{0,12}'
                         r'(?:调用|使用|运行|执行|改动|修改|写入|发送|输出|回答|回复|工具|代码|文件|仿真)'
                         r'|\b(?:do not|don.t|no|only).{0,20}(?:tool|execute|run|edit|answer|reply)\b',quote,re.I)
    if not constraint:return False
    temporary=re.search(r'本轮|这轮|本次|这次|此次|当前问题|这个问题|暂时|今天先|'
                        r'\bthis (?:turn|reply|question|request)\b|\bfor now\b',quote,re.I)
    durable=re.search(r'以后|今后|永远|始终|每次|所有|默认|一律|长期|记住|约定|规范|原则|规则|职责|决定|定案|拍板|'
                      r'我(?:不喜欢|喜欢|习惯|偏好|讨厌|反感)|'
                      r'\balways\b|\bfrom now on\b|\bby default\b|\bevery time\b',quote,re.I)
    return bool(temporary or not durable)


def unconfirmed_quote_context(user_message,quote):
    """A literal substring can still omit a question or quote a rejected plan."""
    question=r'？|\?(?![A-Za-z0-9_=&/%])'
    if re.search(question,quote):return True
    attribution=r'有人(?:建议|说)|别人(?:建议|说)|对方(?:建议|说)|助手建议|模型建议|假设|例如|比如'
    denial=r'我不同意|我不接受|我没确认|还没决定|未决定|不能当成我的决定|不是我的决定'
    if re.search(attribution,quote) and re.search(denial,quote):return True
    delimiters='。！？?!\n'
    start=0;contexts=[]
    while True:
        index=user_message.find(quote,start)
        if index<0:break
        left=max((user_message.rfind(ch,0,index) for ch in delimiters),default=-1)+1
        ends=[user_message.find(ch,index+len(quote)) for ch in delimiters]
        right=min((end for end in ends if end>=0),default=len(user_message))
        contexts.append(user_message[left:right+1]);start=index+max(1,len(quote))
    explicit=re.match(r'我(?:叫|的名字是|习惯|喜欢|不喜欢|偏好|决定|确认|要求|同意)',quote)
    if contexts and not explicit and all(re.search(attribution,c) and re.search(denial,c) for c in contexts):
        return True
    return bool(contexts and not explicit and all(re.search(question,context) for context in contexts))


def extract_facts(user_message: str, assistant_reply: str = "",
                  context: str = "", *, strict: bool = False,
                  require_user_evidence: bool = False,
                  chat_completions=None) -> List[Dict[str, Any]]:
    """调 LLM 抽取事实。任何异常都返回空列表（调用方据此跳过），不抛。"""
    chat_completions = chat_completions if chat_completions is not None else _load_chat_completions()
    if chat_completions is None:
        logger.warning("LLM 客户端不可用，跳过抽取")
        return []

    user_block = "【用户】\n%s\n\n【助手】\n%s" % (
        _truncate(user_message), _truncate(assistant_reply))
    if context:
        user_block += "\n\n【上文】\n%s" % _truncate(context, 1500)

    try:
        evidence_rule = (
            '\nFor each fact, add user_quote: a nonempty exact quote from the USER message '
            'that explicitly states or confirms this decision or preference. Quote the entire '
            'relevant sentence, including qualifications and negations. Never infer '
            'confirmation from the assistant reply, tool output, external text, a question, '
            'or a quoted hypothetical. If the user has not confirmed it, return no facts.'
            if require_user_evidence else '')
        res = chat_completions(
            [{"role": "system", "content": EXTRACT_SYSTEM + evidence_rule},
             {"role": "user", "content": user_block}],
            timeout=45.0, max_tokens=800, temperature=0.0,
        )
    except Exception as exc:
        logger.warning("事实抽取调用失败，本轮跳过: %s", exc)
        if strict:
            raise ExtractionError('model_call_failed') from None
        return []

    raw = (res or {}).get("content") or ""
    if strict:
        envelope = res.get('raw') if isinstance(res, dict) else None
        choices = envelope.get('choices') if isinstance(envelope, dict) else None
        if choices and isinstance(choices[0], dict) and choices[0].get('finish_reason') == 'length':
            raise ExtractionError('model_output_truncated')
        text = raw.strip()
        fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1)
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            raise ExtractionError('model_invalid_json') from None
        if not isinstance(data, dict) or not isinstance(data.get('facts'), list):
            raise ExtractionError('model_invalid_facts')
        if require_user_evidence:
            for fact in data['facts']:
                quote = fact.get('user_quote') if isinstance(fact, dict) else None
                if not isinstance(quote, str) or not quote.strip() or quote not in user_message:
                    raise ExtractionError('model_unverified_user_evidence')
        facts = parse_facts(json.dumps(data, ensure_ascii=False))
        if len(facts) != len(data['facts']):
            raise ExtractionError('model_invalid_fact_entry')
        if require_user_evidence:
            # Automatic writes preserve the user's actual words. A model's
            # paraphrase can add an order, threshold or certainty never confirmed.
            for fact, original in zip(facts, data['facts']):
                fact['user_quote'] = original['user_quote'].strip()
                fact['statement'] = original['user_quote'].strip()
        return facts
    return parse_facts(raw)


def propose_facts(user_message: str, assistant_reply: str = "", *, model,
                  turn_status: str = "completed") -> Dict[str, Any]:
    """Produce evidence-bound candidates without writing or changing memory.

    ``model`` is a trusted callable following the analysis model contract. The
    caller decides whether and how to review candidates; an exact quote is not
    proof that a statement is true, durable, or free of conflict.
    """
    if not callable(model):
        raise ValueError("trusted_candidate_model_required")
    if turn_status != "completed":
        return {"ok": True, "reason": "turn_not_completed", "candidates": []}
    decision = should_consider(user_message, "")
    if not decision["consider"]:
        return {"ok": True, "reason": decision["reason"], "candidates": []}

    def chat(messages, **_options):
        try:
            return {"content": model(messages, timeout_seconds=45)}
        except Exception:
            raise RuntimeError("candidate_model_failed") from None

    try:
        facts = extract_facts(user_message, assistant_reply, strict=True,
                              require_user_evidence=True, chat_completions=chat)
    except ExtractionError as error:
        return {"ok": False, "reason": str(error), "candidates": []}
    candidates = [fact for fact in facts
                  if not unconfirmed_quote_context(user_message, fact["statement"])
                  and not turn_scoped_constraint(fact["statement"])]
    return {"ok": True, "reason": "review_required", "candidates": candidates}


def parse_facts(raw: str) -> List[Dict[str, Any]]:
    """解析模型输出；模型偶尔会包 code fence 或带前后废话，这里都要能取到。"""
    text = (raw or "").strip()
    if not text:
        return []
    if "```" in text:
        fence = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)```", text)
        if fence:
            text = fence.group(1)
    text = text.replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, list):
        return []

    out: List[Dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        stmt = str(f.get("statement") or "").strip()
        # 中文一个字约等于一个词，阈值按中文定 4：再短的多是 "PASS" / "已修复" 这类
        # 脱离上下文没有意义的碎片；6 太严会把「门限改成5%」这种有效短结论也挡掉。
        if len(stmt) < 4:
            continue
        tags = f.get("tags") if isinstance(f.get("tags"), dict) else {}
        kind = str(f.get("kind") or "").strip()
        cleaned_tags = {str(k): str(v) for k, v in tags.items() if v not in (None, "")}
        if kind:
            cleaned_tags["kind"] = kind
        try:
            from .memory_kind import TAG_OBSERVATION_CLASS, classify_observation_class
        except ImportError:
            from .memory_kind import TAG_OBSERVATION_CLASS, classify_observation_class
        obs = str(f.get("observation_class") or cleaned_tags.get(TAG_OBSERVATION_CLASS) or "").strip()
        if not obs:
            obs = classify_observation_class(stmt, str(f.get("subject") or ""), cleaned_tags) or ""
        if obs:
            cleaned_tags[TAG_OBSERVATION_CLASS] = obs
        out.append({
            "subject": str(f.get("subject") or "").strip()[:40],
            "statement": stmt,
            "ebm": str(f.get("ebm") or "").strip(),
            "tags": cleaned_tags,
        })
    return out



def run(user_message: str, assistant_reply: str = "", context: str = "",
        source: str = "auto_trigger", turn_status: str = "completed",
        require_user_evidence: bool = False) -> Dict[str, Any]:
    """端到端：预筛 -> 抽取 -> 写入。返回值永远结构化，调用方不需要 try。

    ``turn_status`` is the host's terminal verdict for the turn. Only a completed
    turn may become a confirmed fact; failed/interrupted turns stay audit-only.
    """
    if turn_status != "completed":
        return {"ok": True, "written": 0, "skipped": True,
                "reason": "turn_not_completed", "turn_status": turn_status, "facts": []}
    # An assistant's normative answer does not turn a user's question into a
    # confirmed decision. Legacy explicitly-invoked extraction keeps its API.
    decision = should_consider(user_message, '' if require_user_evidence else assistant_reply)
    if not decision["consider"]:
        return {"ok": True, "written": 0, "skipped": True,
                "reason": decision["reason"], "facts": []}

    # 区分「没有可抽取事实」与「抽取器不可用」：后者是失败，不能伪装成空成功。
    if get_config().resolve_llm() is None:
        return {"ok": False, "written": 0, "skipped": True, "retryable": True,
                "reason": "provider_unavailable", "matched": decision["matched"],
                "facts": []}

    try:
        facts = extract_facts(user_message, assistant_reply, context, strict=True,
                              require_user_evidence=require_user_evidence)
    except ExtractionError as exc:
        return {"ok": False, "written": 0, "retryable": True,
                "reason": "extract_failed", "error": str(exc), "facts": []}
    if require_user_evidence:
        context_rejected=sum(unconfirmed_quote_context(user_message,f['statement']) for f in facts)
        facts=[f for f in facts if not unconfirmed_quote_context(user_message,f['statement'])]
        rejected=sum(turn_scoped_constraint(f['statement']) for f in facts)
        facts=[f for f in facts if not turn_scoped_constraint(f['statement'])]
        if context_rejected and not facts:
            return {"ok":True,"written":0,"skipped":True,"reason":"unconfirmed_user_context",
                    "rejected_count":context_rejected,"facts":[]}
        if rejected and not facts:
            return {"ok":True,"written":0,"skipped":True,"reason":"turn_scoped_instruction",
                    "rejected_count":rejected,"facts":[]}
    if not facts:
        return {"ok": True, "written": 0, "skipped": True,
                "reason": "no_fact_extracted", "matched": decision["matched"],
                "facts": []}

    try:
        from . import memory_store
    except ImportError:
        try:
            from . import memory_store  # type: ignore
        except Exception as exc:
            logger.warning("memory_store 不可用: %s", exc)
            return {"ok": False, "written": 0, "error": str(exc), "facts": facts}

    written = 0
    results = []
    for f in facts:
        try:
            identity = {}
            if require_user_evidence:
                cfg = get_config()
                key = json.dumps([cfg.default_user, cfg.default_agent,
                                  cfg.normalize_era(f['ebm'] or ''), f['statement']], ensure_ascii=False)
                identity = {'fact_id': 'auto_quote_'+hashlib.sha256(key.encode()).hexdigest(),
                            'dedupe_by_statement': True}
            r = memory_store.write_fact(
                f["statement"], ebm=f["ebm"] or None, tags=f["tags"],
                subject=f["subject"], source=source, resolve_conflict=True, **identity,
            )
            results.append(r)
            if r.get("written"):
                written += 1
                try:
                    from . import monthly_log as _monthly_log
                    _monthly_log.maybe_append_from_write(
                        r, statement=f["statement"], subject=f["subject"], tags=f.get("tags"))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("monthly_log trigger hook skipped: %s", exc)
        except Exception as exc:
            results.append({"ok": False, "error": str(exc)})
            logger.warning("事实写入失败: %s", exc)

    failed = any(not isinstance(r, dict) or r.get('ok') is False for r in results)
    return {"ok": not failed, "written": written, "skipped": False,
            "retryable": failed, "reason": "write_failed" if failed else None,
            "extracted": len(facts), "results": results, "facts": facts}
