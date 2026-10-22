"""Model clients. One OpenAI-compatible adapter for live runs, one mock for --dry-run."""
import json
import os
import random
import threading
import time


def provenance(model_cfg):
    temp = model_cfg.get("temperature")
    extra = model_cfg.get("extra_body") or {}
    return {
        "model": model_cfg["model"],
        "provider": model_cfg.get("provider", ""),
        "temperature": "provider_default" if temp is None else str(temp),
        "reasoning": "provider_default" if not extra else json.dumps(extra, sort_keys=True),
    }


class OpenAICompatClient:
    def __init__(self, model_cfg):
        from openai import OpenAI  # imported here so --dry-run works without the package

        key = os.environ.get(model_cfg["api_key_env"])
        if not key:
            raise SystemExit(
                f"Environment variable {model_cfg['api_key_env']} is not set. "
                f"Set it in PowerShell with:  $env:{model_cfg['api_key_env']}=\"your-key\""
            )
        self.cfg = model_cfg
        self.on_retry = None  # set by the progress display
        self.client = OpenAI(api_key=key, base_url=model_cfg["base_url"],
                             timeout=model_cfg.get("request_timeout_s", 300), max_retries=0)

    def complete(self, messages, max_tokens, kind, expect=None):
        kwargs = dict(model=self.cfg["model"], messages=messages, max_tokens=max_tokens)
        if self.cfg.get("temperature") is not None:
            kwargs["temperature"] = self.cfg["temperature"]
        if self.cfg.get("extra_body"):
            kwargs["extra_body"] = self.cfg["extra_body"]
        last_err = None
        for attempt in range(6):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                break
            except Exception as e:  # network / timeout / rate limit / 5xx
                last_err = e
                wait = min(60, 2 ** attempt + random.random())
                if self.on_retry and attempt < 5:
                    self.on_retry(f"API error ({type(e).__name__}: {str(e)[:80]}); retry {attempt + 1}/5 in {wait:.0f}s")
                if attempt < 5:
                    time.sleep(wait)
        else:
            raise RuntimeError(f"API call failed after 6 attempts: {last_err}")
        choice = resp.choices[0]
        u = resp.usage
        details = getattr(u, "completion_tokens_details", None) if u else None
        return {
            "text": choice.message.content or "",
            "reasoning": getattr(choice.message, "reasoning_content", None),
            "snapshot": getattr(resp, "model", "") or "",
            "finish_reason": choice.finish_reason,
            "usage": {
                "prompt_tokens": getattr(u, "prompt_tokens", None) if u else None,
                "completion_tokens": getattr(u, "completion_tokens", None) if u else None,
                "reasoning_tokens": getattr(details, "reasoning_tokens", None) if details else None,
            },
        }


class MockClient:
    """Returns well-formed fake replies so the whole pipeline can be tested for free."""

    TOPICS = ["the weather", "old movies", "cooking", "space travel", "dogs", "music", "hiking"]

    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.lock = threading.Lock()

    def complete(self, messages, max_tokens, kind, expect=None):
        with self.lock:
            topic = self.rng.choice(self.TOPICS)
            if kind == "conversation":
                text = f"(mock) I've been thinking about {topic} lately. What about you?"
            elif kind in ("rating", "measure"):
                text = json.dumps({k: self.rng.randint(1, 5) for k in expect})
            elif kind == "memory":
                text = json.dumps({
                    "self": f"(mock) I am curious and like talking about {topic}.",
                    "others": {h: f"(mock) seemed interested in {topic}." for h in (expect or [])},
                    "interactions": f"(mock) Recently talked about {topic}.",
                })
            else:
                text = ""
        return {"text": text, "snapshot": "mock", "finish_reason": "stop",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}}
