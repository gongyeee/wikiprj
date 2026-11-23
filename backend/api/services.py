def normalize_search_results(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and (isinstance(data.get("tokenHits"), list) or isinstance(data.get("vectorHits"), list)):
        seen = set()
        merged = []
        for key in ("tokenHits", "vectorHits"):
            for item in data.get(key) or []:
                path = result_path(item) or repr(item)
                if path in seen:
                    continue
                seen.add(path)
                merged.append(item)
        return merged
    for key in ("results", "items", "hits", "tokenHits", "vectorHits"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            return value
    return []


def result_path(result):
    for key in ("path", "filePath", "filepath", "relativePath", "id"):
        value = result.get(key) if isinstance(result, dict) else None
        if isinstance(value, str) and value:
            return value
    return ""


def content_text(payload):
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("content", "text", "markdown", "body", "raw"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""
