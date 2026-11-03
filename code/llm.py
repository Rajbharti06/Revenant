"""Minimal LLM client (stdlib only) with usage accounting and a disk cache.

Providers (picked from whichever key is configured, or forced with LLM_PROVIDER):
  - anthropic: Messages API, key ANTHROPIC_API_KEY
  - nvidia:    NVIDIA hosted API (build.nvidia.com, OpenAI-compatible), key NVIDIA_API_KEY
Keys come from the environment or an uncommitted `.env` at the repository root. With no key every
caller falls back to its deterministic path, so the pipeline always runs end to end.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.request

from ingest import CACHE_DIR, ROOT


def _load_dotenv():
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

PROVIDER = os.environ.get("LLM_PROVIDER") or (
    "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "nvidia" if os.environ.get("NVIDIA_API_KEY") else "none")
API_URL = {"anthropic": "https://api.anthropic.com/v1/messages",
           "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions"}.get(PROVIDER, "")
DEFAULT_MODELS = {"anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5"),
                  "nvidia": ("nvidia/nemotron-3.5-lightning-30b-a3b", "nvidia/nemotron-3.5-super-vl-120b-a12b")}
TEXT_MODEL = os.environ.get("AUDIT_TEXT_MODEL", DEFAULT_MODELS.get(PROVIDER, ("", ""))[0])
VISION_MODEL = os.environ.get("AUDIT_VISION_MODEL", DEFAULT_MODELS.get(PROVIDER, ("", ""))[1])
PROVIDER_NAME = {"anthropic": "Anthropic", "nvidia": "NVIDIA (build.nvidia.com hosted API)"}.get(PROVIDER, "-")

# USD per million tokens (input, output); used only for the usage report estimate. NVIDIA's hosted
# endpoints are used on the free developer tier, so their marginal cost is 0.
PRICES = {"claude-haiku-5-5": (1.0, 5.0), "claude-sonnet-5-5": (3.0, 15.0), "claude-opus-5-5": (5.0, 25.0)}

USAGE: dict = {}  # model -> {"calls", "input_tokens", "output_tokens", "cached_calls"}


def available() -> bool:
    return PROVIDER in ("anthropic", "nvidia")


def _cache_path() -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, "llm_cache.json")


_CACHE: dict | None = None


def _cache() -> dict:
    global _CACHE
    if _CACHE is None:
        p = _cache_path()
        _CACHE = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    return _CACHE


def _save_cache():
    json.dump(_cache(), open(_cache_path(), "w", encoding="utf-8"), indent=1, ensure_ascii=False)


def _account(model, usage, cached=False):
    """Live calls and cache hits are both counted; a cache hit carries the tokens originally spent to
    produce it, so reports show the real cost of an output even when it was replayed from cache."""
    u = USAGE.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_calls": 0,
                                 "cached_input_tokens": 0, "cached_output_tokens": 0})
    if cached:
        u["cached_calls"] += 1
        u["cached_input_tokens"] += usage.get("input_tokens", 0)
        u["cached_output_tokens"] += usage.get("output_tokens", 0)
        return
    u["calls"] += 1
    u["input_tokens"] += usage.get("input_tokens", 0)
    u["output_tokens"] += usage.get("output_tokens", 0)


def call(model: str, system: str, content: list, max_tokens: int = 2000) -> str:
    """content is a list of Anthropic content blocks. Returns the text reply."""
    key = hashlib.sha256(json.dumps([PROVIDER, model, system, content], sort_keys=True).encode()).hexdigest()
    c = _cache()
    if key in c:
        _account(model, c[key].get("usage", {}), cached=True)
        return c[key]["text"]
    if PROVIDER == "anthropic":
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": content}]}
        headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"}
    else:  # OpenAI-compatible chat completions (NVIDIA)
        has_image = any(b.get("type") == "image" for b in content)
        if has_image:
            user = [{"type": "text", "text": b["text"]} if b["type"] == "text" else
                    {"type": "image_url", "image_url": {
                        "url": f"data:{b['source']['media_type']};base64,{b['source']['data']}"}}
                    for b in content]
        else:
            user = "\n\n".join(b["text"] for b in content)
        # extraction/classification needs no visible chain of thought; reasoning models otherwise
        # spend the whole token budget thinking and return empty content
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Authorization": f"Bearer {os.environ['NVIDIA_API_KEY']}", "Accept": "application/json"}
    headers["content-type"] = "application/json"
    for attempt in range(4):
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 400 and body.pop("chat_template_kwargs", None) is not None:
                continue  # endpoint does not accept the thinking switch
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(2 ** attempt * 3)
                continue
            raise
    if PROVIDER == "anthropic":
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        usage = resp.get("usage", {})
    else:
        text = (resp["choices"][0]["message"].get("content") or "")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        u = resp.get("usage", {})
        usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
    _account(model, usage)
    if text.strip():  # never cache an empty answer
        c[key] = {"text": text, "usage": usage}
        _save_cache()
    return text


def image_block(path: str, max_side: int = 1600) -> dict:
    """Base64 image block; large screenshots are re-encoded as JPEG to stay within inline limits."""
    ext = path.rsplit(".", 1)[-1].lower()
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}[ext]
    raw = open(path, "rb").read()
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        raw, media = buf.getvalue(), "image/jpeg"
    except ImportError:
        pass
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(raw).decode()}}


def extract_json(text: str):
    s = text.strip()
    if "```" in s:
        s = s.split("```")[1]
        s = s[4:] if s.startswith("json") else s
    start = min([i for i in (s.find("["), s.find("{")) if i >= 0], default=0)
    return json.loads(s[start:])
