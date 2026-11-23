import hashlib
import json
import re
from pathlib import Path

from django.conf import settings


REGISTRY_PATH = settings.BASE_DIR / "data" / "llm_models.json"


def default_model():
    return {
        "id": "default",
        "name": settings.LLM_MODEL,
        "model": settings.LLM_MODEL,
        "apiBase": settings.LLM_API_BASE,
        "apiKey": settings.LLM_API_KEY,
        "source": "env",
    }


def list_llm_models(include_keys=False):
    models = read_registry().get("models", [])
    if not models:
        models = [default_model()]
    normalized = [normalize_model(item) for item in models if normalize_model(item).get("model")]
    if not any(item.get("id") == "default" for item in normalized):
        normalized.insert(0, default_model())
    if include_keys:
        return normalized
    return [public_model(item) for item in normalized]


def get_llm_model(model_id=None):
    models = list_llm_models(include_keys=True)
    if model_id:
        for item in models:
            if item.get("id") == model_id:
                return item
    return models[0] if models else default_model()


def create_llm_model(payload):
    models = [item for item in list_llm_models(include_keys=True) if item.get("id") != "default"]
    model = normalize_model(payload)
    if not model.get("model"):
        raise ValueError("缺少 model")
    if not model.get("id"):
        model["id"] = stable_model_id(model)
    if model["id"] == "default":
        raise ValueError("default 标识保留给 .env 默认模型")
    if any(item.get("id") == model["id"] for item in models):
        raise ValueError("模型配置已存在")
    models.append(model)
    write_registry({"models": sorted(models, key=lambda item: item.get("name") or item.get("model") or "")})
    return public_model(model)


def update_llm_model(payload):
    model_id = safe_id((payload or {}).get("id") or (payload or {}).get("modelId"))
    if not model_id:
        raise ValueError("缺少 id")
    if model_id == "default":
        raise ValueError("默认模型来自 .env，不能修改")

    models = [item for item in list_llm_models(include_keys=True) if item.get("id") != "default"]
    existing = next((item for item in models if item.get("id") == model_id), None)
    if not existing:
        raise ValueError("模型配置不存在")

    merged = {**existing, **(payload or {}), "id": model_id, "source": "registry"}
    model = normalize_model(merged)
    if not model.get("model"):
        raise ValueError("缺少 model")
    if payload.get("clearApiKey"):
        model["apiKey"] = ""
    elif not str(payload.get("apiKey") or "").strip():
        model["apiKey"] = existing.get("apiKey") or ""

    models = [item for item in models if item.get("id") != model_id]
    models.append(model)
    write_registry({"models": sorted(models, key=lambda item: item.get("name") or item.get("model") or "")})
    return public_model(model)


# 保留旧函数名，供既有调用方兼容使用。
save_llm_model = create_llm_model


def delete_llm_model(model_id):
    if model_id == "default":
        raise ValueError("默认模型来自 .env，不能删除")
    models = [item for item in list_llm_models(include_keys=True) if item.get("id") not in {"default", model_id}]
    write_registry({"models": models})
    return list_llm_models()


def read_registry():
    if not REGISTRY_PATH.exists():
        return {"models": []}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"models": []}
    return data if isinstance(data, dict) else {"models": []}


def write_registry(data):
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_model(payload):
    payload = payload or {}
    model = {
        "id": str(payload.get("id") or "").strip(),
        "name": str(payload.get("name") or payload.get("label") or payload.get("model") or "").strip(),
        "model": str(payload.get("model") or "").strip(),
        "apiBase": str(payload.get("apiBase") or payload.get("baseUrl") or settings.LLM_API_BASE).strip().rstrip("/"),
        "apiKey": str(payload.get("apiKey") or payload.get("key") or "").strip(),
        "source": str(payload.get("source") or "registry").strip(),
    }
    if not model["apiKey"] and model["apiBase"] == settings.LLM_API_BASE:
        model["apiKey"] = settings.LLM_API_KEY
    if not model["name"]:
        model["name"] = model["model"]
    if model["id"]:
        model["id"] = safe_id(model["id"])
    return model


def public_model(model):
    return {
        "id": model.get("id"),
        "name": model.get("name") or model.get("model"),
        "model": model.get("model"),
        "apiBase": model.get("apiBase"),
        "hasApiKey": bool(model.get("apiKey")),
        "source": model.get("source") or "registry",
    }


def stable_model_id(model):
    base = f"{model.get('name')}|{model.get('model')}|{model.get('apiBase')}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return safe_id(f"model-{digest}")


def safe_id(value):
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "").strip())[:80]
