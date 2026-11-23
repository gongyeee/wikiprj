import re
from pathlib import Path, PurePosixPath

from .services import result_path
from .document_conversion import convert_to_markdown


FOCUS_STOP_TERMS = {
    "各自", "分别", "区别", "原则", "规则", "要求", "条件", "流程", "方式",
    "内容", "什么", "哪些", "几种", "几类", "是什么", "有什么", "有哪些",
}


def resolve_raw_source_path(raw_root, source_name):
    raw_root = Path(raw_root).resolve()
    relative = str(source_name or "").strip().replace("\\", "/").lstrip("/")
    if relative.startswith("raw/sources/"):
        relative = relative[len("raw/sources/"):]
    if relative:
        exact = (raw_root / relative).resolve()
        try:
            exact.relative_to(raw_root)
            if exact.is_file():
                return exact
        except ValueError:
            pass
    name = Path(relative).name
    if not name:
        return None
    direct = raw_root / name
    if direct.is_file():
        return direct
    candidates = list(raw_root.rglob(f"{Path(name).stem}.*"))
    return candidates[0] if len(candidates) == 1 else None


def read_raw_source_text(path):
    return convert_to_markdown(path)


def build_terms(query, search_results, documents, expanded_terms):
    terms = []
    for term in extract_focus_terms(query):
        add_term(terms, term, split=False)
    for term in expanded_terms:
        add_term(terms, term, split=False)
    for item in search_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        path_stem = PurePosixPath(result_path(item)).stem
        if item.get("titleMatch") or title in query:
            add_term(terms, title, split=False)
        if path_stem and path_stem in query:
            add_term(terms, path_stem, split=False)
    for doc in documents:
        path_stem = PurePosixPath(doc.get("path", "")).stem
        if path_stem and path_stem in query:
            add_term(terms, path_stem, split=False)
    return terms


def extract_focus_terms(query):
    patterns = [
        r"(?:中|关于|针对)([\u4e00-\u9fffA-Za-z0-9_（）()《》\-]+?)(?:的(?:作用|要求|规则|流程|条件|影响|区别)|是|有哪些|如何|怎么)",
        r"([\u4e00-\u9fffA-Za-z0-9_（）()《》\-]+?)的(?:作用|要求|规则|流程|条件|影响|区别)",
    ]
    terms = []
    for pattern in patterns:
        for match in re.finditer(pattern, query):
            value = match.group(1).strip(" 　，。？！；：、,.?!;:()（）[]【】\"'")
            if "中" in value:
                value = value.split("中")[-1]
            if 2 <= len(value) <= 24 and value not in terms and not is_focus_stop_term(value):
                terms.append(value)
    return terms


def is_focus_stop_term(value):
    clean = str(value or "").strip(" 　，。？！；：、,.?!;:()（）[]【】\"'")
    clean = re.sub(r"^(各自|分别)(的)?", "", clean).strip()
    return not clean or clean in FOCUS_STOP_TERMS or value in FOCUS_STOP_TERMS


def add_term(terms, value, split=True):
    value = str(value or "").strip()
    if len(value) < 2:
        return
    candidates = [value, *(split_query_terms(value) if split else [])]
    seen = {term.casefold() for term in terms}
    for candidate in candidates:
        clean = candidate.strip(" 　，。？！；：、,.?!;:()（）[]【】\"'")
        if len(clean) < 2 or clean.casefold() in seen:
            continue
        terms.append(clean)
        seen.add(clean.casefold())


def split_query_terms(value):
    return [
        part for part in re.split(r"[\s,，。？！；：、,.?!;:()（）\[\]【】]+", value)
        if len(part) >= 2
    ]
