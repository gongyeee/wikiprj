import json
import re
import time

import requests
from django.conf import settings
from urllib3.response import HTTPResponse


SYSTEM_PROMPT = """你是一个严谨的文档问答助手，只能基于用户提供的原文证据回答。
要求：
1. 所有定义、规则、条件、流程、限制和例外都必须由给定原文片段支持。
2. 不要把抽取结果或常识冒充原文事实。
3. 关键结论后用 [1]、[2] 这样的编号引用证据。
4. 不要泛泛建议用户“查阅原文”。只有当上下文都没有覆盖问题要点时，才说明具体缺口。
5. 标记为“命中章节完整原文”或“上级公共内容”的证据是原文，不是抽取摘要。
6. 用户要求“原文、完整条款、完整规定”时，应完整引用相关条款措辞，不得用摘要代替；引用后可以解释。
7. 使用中文回答，除非用户明确要求其他语言。"""


def reasoning_text(message):
    """Compatible gateways use either reasoning_content or reasoning for text."""
    for name in ("reasoning_content", "reasoning"):
        value = message.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def iter_sse_lines(response):
    """Read SSE as bytes and decode only after a complete line is available."""
    def available_chunks():
        raw = getattr(response, "raw", None)
        if isinstance(raw, HTTPResponse) and callable(getattr(raw, "read1", None)):
            # read(None) waits for EOF on non-chunked HTTP responses. read1 returns
            # available bytes without waiting to fill a buffer or finish the body.
            while True:
                chunk = raw.read1(65536, decode_content=True)
                if not chunk:
                    break
                yield chunk
        else:
            # Older urllib3 lacks read1; one byte avoids read-to-fill buffering.
            yield from response.iter_content(chunk_size=1, decode_unicode=False)

    pending = b""
    for chunk in available_chunks():
        if not chunk:
            continue
        pending += chunk
        while b"\n" in pending:
            raw_line, pending = pending.split(b"\n", 1)
            raw_line = raw_line.rstrip(b"\r")
            if raw_line:
                yield raw_line.decode("utf-8", errors="replace")
    if pending:
        yield pending.rstrip(b"\r").decode("utf-8", errors="replace")


def test_llm_model(model_config=None):
    config = llm_config(model_config)
    started = time.perf_counter()
    result = {
        "ok": False,
        "model": config["model"],
        "apiBase": config["apiBase"],
        "elapsedMs": 0,
        "message": "",
        "sample": "",
    }
    if not config["apiKey"]:
        result["message"] = "模型未配置 API Key"
        return result

    url = f"{config['apiBase']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['apiKey']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": "你是模型连通性测试助手。"},
            {"role": "user", "content": "请只回复 OK。"},
        ],
        "temperature": 0,
        "max_tokens": 16,
    }
    try:
        timeout = min(float(settings.LLM_TIMEOUT), 20)
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        result["elapsedMs"] = round((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            result["message"] = f"HTTP {response.status_code}: {response.text[:300]}"
            return result
        data = response.json()
        sample = str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        result["sample"] = sample[:120]
        result["ok"] = bool(sample)
        result["message"] = "模型可用" if result["ok"] else "模型返回为空"
        result["usage"] = data.get("usage")
        return result
    except Exception as exc:
        result["elapsedMs"] = round((time.perf_counter() - started) * 1000)
        result["message"] = str(exc)
        return result


def build_context(documents, max_chars=24000, reserve_chars=2000):
    chunks = []
    used = 0
    documents = [doc for doc in documents if (doc.get("content") or "").strip()]
    reserve_chars = min(reserve_chars, max_chars // max(1, len(documents)))
    for index, doc in enumerate(documents, start=1):
        path = doc.get("path", f"document-{index}")
        text = (doc.get("content") or "").strip()
        if not text:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        reserved = sum(min(reserve_chars, len((other.get("content") or "").strip())) for other in documents[index:])
        clipped = text[:max(0, remaining - reserved)]
        chunks.append(f"[{index}] {path}\n{clipped}")
        used += len(clipped)
    return "\n\n---\n\n".join(chunks)


def build_user_prompt(question, context):
    return f"""用户问题：{question}

原文证据：
{context}

请只基于以上证据生成回答，并在关键结论后标注证据编号。"""


def answer_with_llm(question, documents, model_config=None, context_options=None):
    config = llm_config(model_config)
    context = build_context(documents, **(context_options or {}))
    prompt = build_user_prompt(question, context)

    if not config["apiKey"]:
        return {
            "answer": fallback_answer(question, documents),
            "llmConfigured": False,
            "prompt": prompt,
            "context": context,
            "model": config["model"],
        }

    url = f"{config['apiBase']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['apiKey']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=settings.LLM_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        answer = data["choices"][0]["message"]["content"]
        if not str(answer or "").strip():
            raise ValueError("模型返回空回答")
        return {
            "answer": answer,
            "llmConfigured": True,
            "prompt": prompt,
            "context": context,
            "model": config["model"],
            "usage": data.get("usage"),
        }
    except Exception as exc:
        return {
            "answer": fallback_answer(question, documents),
            "llmConfigured": False,
            "llmError": str(exc),
            "prompt": prompt,
            "context": context,
            "model": config["model"],
        }


def stream_answer_with_llm(question, documents, model_config=None, context_options=None):
    config = llm_config(model_config)
    context = build_context(documents, **(context_options or {}))
    prompt = build_user_prompt(question, context)
    meta = {
        "llmConfigured": bool(config["apiKey"]),
        "prompt": prompt,
        "context": context,
        "model": config["model"],
    }
    if not config["apiKey"]:
        yield {"event": "meta", **meta}
        yield {"event": "chunk", "content": fallback_answer(question, documents)}
        yield {"event": "done", "answer": fallback_answer(question, documents), **meta}
        return

    url = f"{config['apiBase']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['apiKey']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "stream": True,
    }
    answer_parts = []
    yield {"event": "meta", **meta}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=settings.LLM_TIMEOUT, stream=True)
        response.raise_for_status()
        for raw_line in iter_sse_lines(response):
            line = raw_line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            for choice in data.get("choices", []):
                delta = choice.get("delta") or {}
                reasoning = reasoning_text(delta)
                if reasoning:
                    yield {"event": "thinking", "content": reasoning}
                content = delta.get("content")
                if content is None:
                    content = (choice.get("message") or {}).get("content")
                if content:
                    answer_parts.append(content)
                    yield {"event": "chunk", "content": content}
        answer = "".join(answer_parts).strip()
        if not answer:
            raise ValueError("模型返回空回答")
        yield {"event": "done", "answer": answer, **meta}
    except Exception as exc:
        answer = fallback_answer(question, documents)
        if answer:
            yield {"event": "chunk", "content": answer}
        yield {"event": "done", "answer": answer, "llmError": str(exc), **{**meta, "llmConfigured": False}}


def fallback_answer(question, documents):
    if not documents:
        return ""

    lines = [
        "未启用可用 LLM，已按当前选择的证据文档直接拼接如下。",
        "",
        f"问题：{question}",
        "",
    ]
    used_chars = 0
    for index, doc in enumerate(documents, start=1):
        text = str(doc.get("content") or "").strip()
        if not text:
            continue
        path = doc.get("path", f"document-{index}")
        remaining = 6000 - used_chars
        if remaining <= 0:
            break
        excerpt = text[:remaining]
        lines.append(f"## [{index}] {path}")
        lines.append("")
        lines.append(excerpt)
        lines.append("")
        used_chars += len(excerpt)
    if not used_chars:
        return ""
    lines.append("> 以上为本地兜底拼接内容；配置可用模型后可生成综合回答。")
    return "\n".join(lines).strip()


def rerank_search_results_with_llm(question, results, threshold=0.45, model_config=None):
    config = llm_config(model_config)
    if not config["apiKey"] or not results:
        return {
            "enabled": False,
            "reason": "LLM_API_KEY 未配置" if not config["apiKey"] else "没有候选结果",
            "stage": "source-chunk",
            "candidateCount": len(results),
            "keptCount": len(results),
            "results": results,
        }

    candidates = []
    for index, item in enumerate(results[:20]):
        candidates.append(
            {
                "index": index,
                "title": item.get("title") or item.get("name") or item.get("path") or "",
                "path": item.get("path") or item.get("filePath") or item.get("relativePath") or item.get("id") or "",
                "snippet": (item.get("evidenceSnippet") or item.get("snippet") or item.get("summary") or "")[:700],
            }
        )

    prompt = f"""请判断候选文档与用户查询的语义相关性。
用户查询：{question}

候选文档 JSON：
{json.dumps(candidates, ensure_ascii=False)}

只返回 JSON 数组，每个元素格式为：
{{"index": 数字, "score": 0到1的小数, "reason": "不超过20字"}}

评分标准：
- 1.0 表示直接回答查询核心对象。
- 0.7 表示强相关但偏背景或机制。
- 0.4 表示同领域或上下位相关。
- 0.2 以下表示基本无关。"""
    data = chat_json(prompt, config)
    scores = {}
    if isinstance(data, list):
        for item in data:
            try:
                scores[int(item.get("index"))] = {
                    "score": float(item.get("score", 0)),
                    "reason": str(item.get("reason", "")),
                }
            except (TypeError, ValueError):
                continue

    filtered = []
    for index, item in enumerate(results):
        score_info = scores.get(index)
        if not score_info:
            item["llmRelevance"] = None
            filtered.append(item)
            continue
        item["llmRelevance"] = score_info["score"]
        item["llmRelevanceReason"] = score_info["reason"]
        if score_info["score"] >= threshold:
            filtered.append(item)

    return {
        "enabled": True,
        "threshold": threshold,
        "model": config["model"],
        "stage": "source-chunk",
        "candidateCount": len(results),
        "scoredCount": len(scores),
        "keptCount": len(filtered),
        "results": filtered,
    }


def analyze_query_intent(question, model_config=None):
    config = llm_config(model_config)
    question = str(question or "").strip()
    fallback = fallback_query_intent(question)
    if not config["apiKey"] or not question:
        fallback["enabled"] = False
        fallback["reason"] = "LLM_API_KEY 未配置" if question else "空问题"
        return fallback

    prompt = f"""请分析用户检索问题，提取适合文档检索的意图。

用户问题：{question}

本地分词候选（仅供参考，词性不能直接决定核心对象）：{json.dumps(fallback.get("segmentation", []), ensure_ascii=False)}

只返回合法 JSON 对象，字段如下：
{{
  "intent": "definition|rule|classification|comparison|procedure|condition|general",
  "focusTerms": ["核心对象或术语"],
  "searchTerms": ["适合检索的关键词或短语"],
  "coreObjects": ["真正被询问的对象，必须使用用户原话，比较问题保留多个对象；不确定可为空"],
  "scopeTerms": ["业务、文档、所属章节或运行环境限定，使用用户原话"],
  "requestedAttribute": "被询问的属性，如文件格式、配置、作用",
  "rewrittenQuery": "用于检索的简洁查询，不超过80字",
  "evidencePreference": "source"
}}

要求：
- 去掉“是什么、有哪些、各自、区别、原则、请问”等问题意图虚词。
- 保留用户真正想查的对象，例如“撮合规则”“连续竞价”“集合竞价”“股票分类”。
- 对简单的“对象＋属性”组合，同时区分对象和属性：例如“撮合规则”的 coreObjects 为 ["撮合"]，requestedAttribute 为“规则”，searchTerms 保留“撮合规则”。不要生成“合规”等跨词字符片段，也不要拆开引号中的专有名称或文件标识符。
- 区分核心对象和范围。例如“新港股通中RC主机上的app_shell文件格式”：核心对象为app_shell，范围为新港股通、RC主机，属性为文件格式。保留文件名完整形式，不拆开下划线；“RC如何配置”的核心对象则为RC。
- 可以补充少量同义或上下位检索词，但不要泛化到无关概念。
- 如果是“各自区别/原则”类问题，应保留多个对象及“区别、原则”对应的实质检索词。"""
    model_started = time.perf_counter()
    try:
        data = chat_json(prompt, config, timeout=(5, settings.QUERY_INTENT_TIMEOUT),
                         max_tokens=settings.QUERY_INTENT_MAX_TOKENS)
    except Exception as exc:
        fallback["enabled"] = False
        fallback["reason"] = f"LLM 意图识别失败：{exc}"
        fallback["modelCallElapsedMs"] = round((time.perf_counter() - model_started) * 1000, 2)
        return fallback

    if not isinstance(data, dict):
        fallback["enabled"] = False
        fallback["reason"] = "LLM 意图识别返回格式异常"
        fallback["modelCallElapsedMs"] = round((time.perf_counter() - model_started) * 1000, 2)
        return fallback

    focus_terms = clean_terms(data.get("focusTerms", []))
    search_terms = clean_terms(data.get("searchTerms", []))
    rewritten_query = str(data.get("rewrittenQuery") or "").strip()
    if not rewritten_query:
        rewritten_query = fallback["rewrittenQuery"]
    expanded_query = expand_query_with_terms(question, [rewritten_query, *focus_terms, *search_terms])
    from .query_roles import query_roles
    return {
        **query_roles(question, data),
        "enabled": True,
        "reason": "",
        "intent": normalize_intent(data.get("intent")),
        "modelCallElapsedMs": round((time.perf_counter() - model_started) * 1000, 2),
        "focusTerms": focus_terms,
        "searchTerms": search_terms,
        "rewrittenQuery": rewritten_query[:120],
        "expandedQuery": expanded_query,
        "evidencePreference": normalize_evidence_preference(data.get("evidencePreference")),
        "model": config["model"],
    }


def fallback_query_intent(question):
    from .query_roles import query_roles
    terms = clean_terms(split_query_for_intent(question))
    rewritten = " ".join(terms[:8]) or question
    return {
        **query_roles(question),
        "enabled": False,
        "reason": "规则降级",
        "intent": infer_intent(question),
        "focusTerms": terms[:4],
        "searchTerms": terms[:8],
        "rewrittenQuery": rewritten[:120],
        "expandedQuery": expand_query_with_terms(question, terms),
        "evidencePreference": infer_evidence_preference(question),
        "model": "",
    }


def split_query_for_intent(question):
    text = str(question or "").strip()
    cleaned = re.sub(r"^(什么是|何为|查询|检索|解释|说明|关于|请问)", "", text).strip()
    cleaned = re.sub(
        r"(是什么|是啥|是什么意思|有哪些|有哪[几些]?(种|类|型)|哪[几些]?(种|类|型)|几(种|类|型)|多少(种|类|型)|如何分类|怎么分类|分类是什么|类型有哪些|种类有哪些|类别有哪些|有哪些类型|有哪些种类|有哪些类别|如何|怎么|为何|为什么)$",
        "",
        cleaned,
    ).strip()
    parts = [cleaned, text]
    parts.extend(re.split(r"[\s,，。？?！!；;：:、()（）\[\]【】《》<>\"']+", text))
    if "撮合" in text:
        parts.extend(["撮合", "竞价", "成交", "连续竞价", "集合竞价", "价格优先", "时间优先"])
    return parts


def clean_terms(values):
    if isinstance(values, str):
        values = re.split(r"[,，、;；\s]+", values)
    if not isinstance(values, list):
        return []
    stop_terms = {
        "是什么", "什么", "哪些", "各自", "分别", "区别", "原则", "规则", "内容",
        "请问", "查询", "检索", "说明", "解释", "有哪些", "有几种", "有几类",
    }
    terms = []
    seen = set()
    for value in values:
        clean = str(value or "").strip(" \t\r\n，。？！；：、,.?!;:()（）[]【】\"'")
        clean = re.sub(r"^(请问|查询|检索|解释|说明|关于)", "", clean).strip()
        clean = re.sub(r"(是什么|是啥|是什么意思|有哪些)$", "", clean).strip()
        if len(clean) < 2 or clean in stop_terms:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(clean[:40])
    return terms[:16]


def expand_query_with_terms(question, terms):
    pieces = [str(question or "").strip()]
    seen = {pieces[0].casefold()} if pieces[0] else set()
    for term in clean_terms(terms):
        key = term.casefold()
        if key in seen or key in str(question or "").casefold():
            continue
        pieces.append(term)
        seen.add(key)
    return " ".join(piece for piece in pieces if piece).strip()


def infer_intent(question):
    text = str(question or "")
    if re.search(r"哪[几些]?(种|类|型)|几(种|类|型)|分类|类型|种类|类别", text):
        return "classification"
    if re.search(r"区别|不同|对比|比较|各自", text):
        return "comparison"
    if re.search(r"流程|步骤|如何|怎么", text):
        return "procedure"
    if re.search(r"条件|要求|限制|门槛", text):
        return "condition"
    if re.search(r"规则|原则|机制|规定", text):
        return "rule"
    if re.search(r"是什么|定义|概念|含义|意思", text):
        return "definition"
    return "general"


def normalize_intent(value):
    value = str(value or "general").strip().lower()
    allowed = {"definition", "rule", "classification", "comparison", "procedure", "condition", "general"}
    return value if value in allowed else "general"


def infer_evidence_preference(question):
    return "source"


def normalize_evidence_preference(value):
    return "source"


def chat_json(prompt, model_config=None, *, timeout=None, max_tokens=None):
    config = llm_config(model_config)
    url = f"{config['apiBase']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['apiKey']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": "你只输出合法 JSON，不要输出 Markdown。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    response = requests.post(url, headers=headers, json=payload,
                             timeout=settings.LLM_TIMEOUT if timeout is None else timeout)
    try:
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("JSON 输出达到 token 上限")
        text = choice["message"]["content"].strip()
    finally:
        response.close()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def llm_config(model_config=None):
    model_config = model_config or {}
    return {
        "model": model_config.get("model") or settings.LLM_MODEL,
        "apiBase": (model_config.get("apiBase") or settings.LLM_API_BASE).rstrip("/"),
        "apiKey": model_config.get("apiKey") if model_config.get("apiKey") is not None else settings.LLM_API_KEY,
    }
