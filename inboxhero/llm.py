"""Model access.

Three properties this client enforces, all of which matter more than which
provider is behind it.

*The model returns data, never a decision to act.* Every call goes through
`complete_json`, which demands an object matching a caller-supplied schema and
discards anything that does not fit. There is no code path where model output
selects a function, names a recipient, or reaches tools.py.

*A model is an improvement, not a dependency.* Every caller has a deterministic
baseline that runs first. The model refines wording and fills in judgement
calls. When it is absent, unreachable, rate-limited, or returns nonsense, the
baseline stands and the run completes. No safety property -- what gets gated,
refused, or flagged -- is decided by the model, so losing it degrades quality
and nothing else.

Standard library only: no dependency needs installing for this to run.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import tracing


class LLMUnavailable(RuntimeError):
    """Raised internally when a provider cannot answer. Callers see None."""


class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cache_dir = Path(cfg.paths["cache"])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_call = 0.0
        self.calls_made = 0
        self.cache_hits = 0
        self.failures = 0
        # Set once a provider has proven unreachable, so a 100-message run does
        # not spend its life timing out against a server that is not there.
        self._disabled_reason: str | None = None

    # -- public ----------------------------------------------------------

    @property
    def available(self) -> bool:
        return not self.cfg.is_offline and self._disabled_reason is None

    def status(self) -> str:
        if self.cfg.is_offline:
            return "offline (deterministic baselines only, by configuration)"
        if self._disabled_reason:
            return f"unavailable: {self._disabled_reason} -- deterministic baselines only"
        return f"{self.cfg.provider}:{self.cfg.model}"

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        prompt: str,
        schema: dict[str, type],
        cap: str | None = None,
    ) -> dict[str, Any] | None:
        """Ask for one JSON object. Returns None rather than raising, ever.

        `schema` maps field name to expected Python type. Fields that are
        missing or of the wrong type are dropped. A caller therefore never
        sees a value it did not ask for, which is what keeps model output from
        widening into something that could name an action.
        """
        if not self.available:
            return None

        key = self._cache_key(task, system, prompt)
        cached = self._cache_read(key)
        if cached is not None:
            self.cache_hits += 1
            tracing.emit("llm_call", cap=cap, task=task, cached=True, model=self.cfg.model)
            return self._coerce(cached, schema)

        try:
            raw = self._call_with_retries(system, prompt)
        except LLMUnavailable as exc:
            self.failures += 1
            self._disabled_reason = str(exc)
            tracing.emit("llm_unavailable", cap=cap, task=task, reason=str(exc))
            return None

        parsed = _extract_json(raw)
        if parsed is None:
            self.failures += 1
            tracing.emit("llm_call", cap=cap, task=task, ok=False, reason="unparseable response")
            return None

        self._cache_write(key, parsed)
        self.calls_made += 1
        tracing.emit("llm_call", cap=cap, task=task, cached=False, model=self.cfg.model)
        return self._coerce(parsed, schema)

    # -- providers -------------------------------------------------------

    def _call_with_retries(self, system: str, prompt: str) -> str:
        delay = 2.0
        last_error = ""
        for attempt in range(self.cfg.max_retries):
            self._pace()
            try:
                return self._call_once(system, prompt)
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                # 429 is the free-tier signal; 5xx is worth one more try.
                if exc.code == 429 or 500 <= exc.code < 600:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                    time.sleep(min(wait, 30.0))
                    delay *= 2
                    continue
                raise LLMUnavailable(f"HTTP {exc.code} from {self.cfg.provider}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(getattr(exc, "reason", exc))
                # Nothing listening: retrying will not help.
                raise LLMUnavailable(f"cannot reach {self.cfg.provider} ({last_error})") from exc
        raise LLMUnavailable(f"{self.cfg.provider} still failing after retries ({last_error})")

    def _call_once(self, system: str, prompt: str) -> str:
        provider = self.cfg.provider
        if provider == "ollama":
            return self._ollama(system, prompt)
        if provider == "gemini":
            return self._gemini(system, prompt)
        if provider == "openai":
            return self._openai(system, prompt)
        raise LLMUnavailable(f"unknown provider {provider!r}")

    def _ollama(self, system: str, prompt: str) -> str:
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": self.cfg.temperature},
        }
        body = self._post(f"{self.cfg.ollama_host.rstrip('/')}/api/chat", payload)
        return body.get("message", {}).get("content", "")

    def _gemini(self, system: str, prompt: str) -> str:
        if not self.cfg.api_key:
            raise LLMUnavailable("no API key set for gemini")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.cfg.model}:generateContent?key={self.cfg.api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.cfg.temperature,
                "responseMimeType": "application/json",
            },
        }
        body = self._post(url, payload)
        try:
            return body["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return ""

    def _openai(self, system: str, prompt: str) -> str:
        if not self.cfg.api_key:
            raise LLMUnavailable("no API key set for openai")
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.cfg.temperature,
            "response_format": {"type": "json_object"},
        }
        body = self._post(
            "https://api.openai.com/v1/chat/completions",
            payload,
            headers={"Authorization": f"Bearer {self.cfg.api_key}"},
        )
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return ""

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=self.cfg.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    # -- plumbing --------------------------------------------------------

    def _pace(self) -> None:
        interval = self.cfg.request_interval
        if interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_call = time.monotonic()

    def _cache_key(self, task: str, system: str, prompt: str) -> str:
        blob = f"{self.cfg.provider}|{self.cfg.model}|{task}|{system}|{prompt}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]

    def _cache_read(self, key: str) -> dict | None:
        if not self.cfg.use_cache:
            return None
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _cache_write(self, key: str, value: dict) -> None:
        if not self.cfg.use_cache:
            return
        try:
            (self.cache_dir / f"{key}.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def _coerce(parsed: dict, schema: dict[str, type]) -> dict[str, Any]:
        """Keep only the fields asked for, only when the type is right."""
        clean: dict[str, Any] = {}
        for field, expected in schema.items():
            if field not in parsed:
                continue
            value = parsed[field]
            if expected is str and isinstance(value, (int, float)):
                value = str(value)
            if isinstance(value, expected):
                clean[field] = value
        return clean


def _extract_json(raw: str) -> dict | None:
    """Pull one JSON object out of a model response, tolerating stray prose."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        _, _, raw = raw.partition("\n")
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(raw[start : end + 1])
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None
