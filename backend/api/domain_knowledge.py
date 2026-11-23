from pathlib import Path
import hashlib
import re

from django.conf import settings
import yaml


DEFAULT_SYNONYMS = {
    "登录": ["登陆", "用户登录", "用户登陆", "认证入口"],
    "鉴权": ["权限校验", "权限验证", "auth", "authorization"],
    "接口": ["API", "服务接口", "HTTP接口"],
    "客户": ["用户", "账户主体"],
    "撮合": ["竞价", "成交", "价格优先", "时间优先", "连续竞价", "集合竞价"],
}


def project_layer_dir(project_id):
    digest = hashlib.sha1(project_id.encode("utf-8")).hexdigest()[:16]
    return Path(settings.DOMAIN_LAYER_DIR) / digest


def ensure_project_layer(project_id):
    layer_dir = project_layer_dir(project_id)
    layer_dir.mkdir(parents=True, exist_ok=True)

    synonym_path = layer_dir / "synonym.yaml"
    if not synonym_path.exists():
        save_synonyms(project_id, DEFAULT_SYNONYMS)

    return layer_dir


def synonym_path(project_id):
    return ensure_project_layer(project_id) / "synonym.yaml"


def normalize_synonyms(data):
    normalized = {}
    if not isinstance(data, dict):
        return normalized

    for raw_key, raw_values in data.items():
        key = str(raw_key).strip()
        if not key:
            continue

        if isinstance(raw_values, str):
            values = split_terms(raw_values)
        elif isinstance(raw_values, list):
            values = [str(value).strip() for value in raw_values]
        else:
            values = []

        seen = {key.casefold()}
        clean_values = []
        for value in values:
            if not value:
                continue
            folded = value.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            clean_values.append(value)

        normalized[key] = clean_values

    return normalized


def load_synonyms(project_id):
    path = synonym_path(project_id)
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = yaml.safe_load(text) or {}
    return normalize_synonyms(data)


def save_synonyms(project_id, synonyms):
    path = project_layer_dir(project_id)
    path.mkdir(parents=True, exist_ok=True)
    normalized = normalize_synonyms(synonyms)
    yaml_text = yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False)
    (path / "synonym.yaml").write_text(yaml_text, encoding="utf-8")
    return normalized


def dump_synonyms(synonyms):
    return yaml.safe_dump(normalize_synonyms(synonyms), allow_unicode=True, sort_keys=False)


def split_terms(text):
    parts = re.split(r"[,，、;；\n]+", text)
    return [part.strip().strip("-").strip() for part in parts if part.strip().strip("-").strip()]


def parse_synonym_text(text):
    stripped = text.strip()
    if not stripped:
        return {}

    try:
        loaded = yaml.safe_load(stripped)
        if isinstance(loaded, dict):
            return normalize_synonyms(loaded)
    except yaml.YAMLError:
        pass

    groups = {}
    current_key = ""
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if ":" in line:
            key, rest = line.split(":", 1)
            current_key = key.strip()
            if current_key:
                groups.setdefault(current_key, [])
                groups[current_key].extend(split_terms(rest))
            continue

        if current_key and line.startswith("-"):
            term = line[1:].strip()
            if term:
                groups[current_key].append(term)

    return normalize_synonyms(groups)


def merge_synonyms(base, incoming):
    merged = {key: list(values) for key, values in normalize_synonyms(base).items()}
    term_to_key = {}

    for key, values in merged.items():
        for term in [key, *values]:
            term_to_key[term.casefold()] = key

    for incoming_key, incoming_values in normalize_synonyms(incoming).items():
        all_terms = [incoming_key, *incoming_values]
        target_key = next((term_to_key[term.casefold()] for term in all_terms if term.casefold() in term_to_key), incoming_key)
        merged.setdefault(target_key, [])
        known = {target_key.casefold(), *(term.casefold() for term in merged[target_key])}

        for term in all_terms:
            folded = term.casefold()
            if folded in known:
                continue
            merged[target_key].append(term)
            known.add(folded)
            term_to_key[folded] = target_key

    return normalize_synonyms(merged)


def expand_query_with_synonyms(project_id, query):
    synonyms = load_synonyms(project_id)
    query_folded = query.casefold()
    matched_groups = []
    expanded_terms = []

    for key, values in synonyms.items():
        terms = [key, *values]
        if any(term.casefold() in query_folded for term in terms):
            matched_groups.append({"canonical": key, "terms": terms})
            for term in terms:
                if term.casefold() not in {item.casefold() for item in expanded_terms}:
                    expanded_terms.append(term)

    if not expanded_terms:
        return {
            "query": query,
            "expandedQuery": query,
            "expandedTerms": [],
            "matchedGroups": [],
        }

    expanded_query = f"{query} {' '.join(expanded_terms)}"
    return {
        "query": query,
        "expandedQuery": expanded_query,
        "expandedTerms": expanded_terms,
        "matchedGroups": matched_groups,
    }
