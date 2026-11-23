"""Conservative query roles: only user-written terms may become constraints."""
import re
from functools import lru_cache

import jieba
import jieba.posseg


ATTRIBUTE_WORDS = {"规则", "流程", "文件格式", "格式", "配置", "要求", "条件", "原理", "作用", "定义", "方法", "步骤", "说明"}
STOP_WORDS = {"而言", "来说", "什么", "如何", "怎么", "哪些", "以及", "对于", "关于", "针对", "请问", "是否", "一个", "进行", "相关", "内容"}


@lru_cache(maxsize=4)
def domain_tokenizer(domain_terms):
    """Independent dictionaries prevent one project's words leaking into another."""
    tokenizer = jieba.Tokenizer()
    tokenizer.initialize()
    for term in domain_terms:
        tokenizer.add_word(term, freq=100000000, tag="nz")
    return jieba.posseg.POSTokenizer(tokenizer)


def segment_question(question, domain_terms):
    dictionary = tuple(sorted(set(term for term in domain_terms if valid_phrase(term))))
    tokenizer = domain_tokenizer(dictionary)
    # Preserve quoted names and file/field identifiers regardless of dictionary entries.
    parts = re.split(r'([A-Za-z][A-Za-z0-9]*(?:[_.-][A-Za-z0-9]+)+|[“"「`][^”"」`]{2,80}[”"」`])', question)
    tokens = []
    for index, part in enumerate(parts):
        if index % 2:
            tokens.append({"text": part.strip('“”"「」`'), "pos": "identifier", "protected": True})
        else:
            tokens.extend({"text": word, "pos": flag, "protected": word in dictionary}
                          for word, flag in tokenizer.cut(part, HMM=False) if word.strip())
    for token in tokens:
        word = token["text"]
        token["role"] = ("protected" if token["protected"] else "attribute" if word in ATTRIBUTE_WORDS
                         else "candidate" if valid_phrase(word) and word not in STOP_WORDS
                         and token["pos"].startswith(("n", "v", "a", "eng")) else "ignored")
    return tokens


def meaningful_phrases(question):
    # Split grammatical boundaries, never sliding character windows.
    text = re.sub(r"^(请问|请解释|请介绍|关于|针对|什么是|查询|检索)", "", str(question).strip())
    text = re.sub(r"(是什么|有哪些|有什么|怎么样|如何|怎么|为什么)[？?。!！]*$", "", text)
    text = re.sub(r"统计(?=的|$)", " ", text)
    values = re.split(r"而言|来说|对于|关于|针对|以及|或者|中的|上的|下的|的|[，,。；;：:？！?!\s]+", text)
    return list(dict.fromkeys(value.strip() for value in values if valid_phrase(value.strip())))


def valid_phrase(value):
    return (2 <= len(value) <= 80 and value not in {"而言", "来说", "什么", "如何", "怎么", "哪些", "以及", "对于", "关于", "针对"}
            and not value.startswith(("的", "而", "了")) and not value.endswith(("的", "而")))


def query_roles(question, proposed=None, domain_terms=None):
    question = str(question or "")
    proposed = proposed if isinstance(proposed, dict) else {}
    def grounded(name):
        values = proposed.get(name, [])
        if not isinstance(values, list):
            return []
        return list(dict.fromkeys(
            value.strip() for value in values if isinstance(value, str)
            and valid_phrase(value.strip()) and value.strip().casefold() in question.casefold()
        ))[:8]
    identifiers = re.findall(r"[A-Za-z][A-Za-z0-9]*(?:[_.-][A-Za-z0-9]+)+", question)
    quoted = re.findall(r'[“"「`]([^”"」`]{2,80})[”"」`]', question)
    core = grounded("coreObjects") or list(dict.fromkeys(identifiers + quoted))
    scope = grounded("scopeTerms")
    if not scope and core:
        prefix = question[:min(question.casefold().find(term.casefold()) for term in core)]
        scope = [value.strip() for value in re.split(r"中|上的|下的|下|上|的", prefix) if len(value.strip()) >= 2][:8]
    attribute = proposed.get("requestedAttribute", "")
    if not isinstance(attribute, str) or attribute.casefold() not in question.casefold():
        attribute = ""
    if not attribute:
        attribute = next((value for value in ("文件格式", "格式", "配置", "区别", "作用", "流程") if value in question), "")
    phrases = list(dict.fromkeys([*core, *scope, *([attribute] if valid_phrase(attribute) else []), *grounded("focusTerms"), *grounded("searchTerms"), *meaningful_phrases(question)]))[:24]
    compound = None
    token_fallback = None
    question_phrases = meaningful_phrases(question)
    tokens = segment_question(question, domain_terms or ())
    if not identifiers and not quoted and len(question_phrases) == 1:
        phrase = question_phrases[0]
        phrase_tokens = segment_question(phrase, domain_terms or ())
        if len(phrase_tokens) == 1 and phrase_tokens[0]["protected"]:
            core = [phrase]
            phrases = [term for term in phrases if term == phrase or term not in phrase]
            phrases.insert(0, phrase)
        if len(phrase_tokens) >= 2 and phrase_tokens[-1]["role"] == "attribute":
            prefix = phrase_tokens[:-1]
            obj = "".join(token["text"] for token in prefix)
            if all(token["role"] in {"candidate", "protected"} for token in prefix) and valid_phrase(obj):
                compound = {"phrase": phrase, "object": obj, "attribute": phrase_tokens[-1]["text"]}
                # Explicit model interpretation wins when it identifies a different object.
                if not core or core == [phrase] or core == [obj]:
                    core, attribute = [obj], compound["attribute"]
                else:
                    compound = None
        if not compound and len(phrase_tokens) >= 2 and all(token["role"] in {"candidate", "protected"} for token in phrase_tokens):
            candidates = list(dict.fromkeys(token["text"] for token in phrase_tokens))
            if 2 <= len(candidates) <= 6 and not core:
                token_fallback = {"phrase": phrase, "terms": candidates}
    return {"coreObjects": core, "scopeTerms": scope, "requestedAttribute": attribute,
            "segmentation": tokens, "tokenizer": "jieba+domain", "domainTermCount": len(domain_terms or ()),
            "compoundFallback": compound,
            "tokenFallback": token_fallback,
            "phrases": phrases, "originalQuestion": question,
            "coreConfidence": "high" if core and all(term in identifiers + quoted for term in core) else "medium" if core else "low"}


def contains_term(text, term):
    if re.fullmatch(r"[A-Za-z0-9_.-]+", term):
        return re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(term) + r"(?![A-Za-z0-9_.-])", text, re.I) is not None
    return term.casefold() in text.casefold()
