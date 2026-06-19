"""
LLM backend hook + a local, zero-cost implementation.

Nodes run on heuristics by default. A backend implementing the LLMClassifier
Protocol can be passed into a node constructor; the node uses the LLM output
when present and falls back to heuristics if the backend raises.

`OllamaClassifier` (below) is the default upgrade path: it runs a LOCAL model
via Ollama — 100% offline, no cloud, no API key, no metered cost. The only
network touch is the loopback connection to the Ollama daemon on this machine.

To enable it, set STRATA_LLM_BACKEND=local (and `pip install ollama`, with a
model pulled, e.g. `ollama pull llama3.2:3b`). Left unset, the service runs
exactly as before on heuristics — so this file adds capability without changing
default behavior or requiring any new dependency.

A cloud backend (e.g. Anthropic) can be added later by writing another class
with the same `.classify(text, schema) -> dict` method and selecting it here.
"""
from __future__ import annotations
import json
import os
from typing import Any, Optional, Protocol, runtime_checkable

# Optional dependency: importing must never break the service when absent.
try:
    import ollama
    _OLLAMA_IMPORTED = True
except Exception:
    _OLLAMA_IMPORTED = False


@runtime_checkable
class LLMClassifier(Protocol):
    """Backend that returns a JSON-shaped dict for a given text + schema hint."""

    def classify(self, text: str, schema: str) -> dict[str, Any]: ...


class OllamaClassifier:
    """LLMClassifier backed by a LOCAL Ollama model (offline, no API key).

    Returns a JSON dict matching the requested schema. Raises on any failure so
    the calling node transparently falls back to its heuristics.
    """

    def __init__(self, model: Optional[str] = None,
                 num_ctx: Optional[int] = None,
                 keep_alive: Optional[str] = None):
        if not _OLLAMA_IMPORTED:
            raise RuntimeError("ollama package not installed")
        self.model = model or os.environ.get("STRATA_MODEL", "llama3.2:3b")
        # Bound the KV cache so the model fits low-RAM machines (128K default OOMs).
        self.num_ctx = int(num_ctx or os.environ.get("STRATA_NUM_CTX", "2048"))
        # Keep the model resident between calls so only the first pays cold-load.
        self.keep_alive = keep_alive or os.environ.get("STRATA_KEEP_ALIVE", "10m")

    def available(self) -> bool:
        """True only if the daemon is reachable and the model is pulled."""
        try:
            data = ollama.list()
            models = getattr(data, "models", None)
            if models is None and isinstance(data, dict):
                models = data.get("models", [])
            names = []
            for m in (models or []):
                n = getattr(m, "model", None)
                if n is None and isinstance(m, dict):
                    n = m.get("model") or m.get("name")
                if n:
                    names.append(n)
            return any(self.model.split(":")[0] in n for n in names)
        except Exception:
            return False

    def classify(self, text: str, schema: str) -> dict[str, Any]:
        resp = ollama.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": (
                    "You are a strict text classifier. Return ONLY a JSON object "
                    f"matching this schema (these keys, lowercase enum-style string "
                    f"values): {schema}. No prose, no explanation."
                )},
                {"role": "user", "content": text},
            ],
            format="json",            # force valid JSON output
            keep_alive=self.keep_alive,
            options={"temperature": 0.0, "num_ctx": self.num_ctx, "num_predict": 128},
        )
        return json.loads(resp["message"]["content"])


def load_classifier_from_env() -> Optional[LLMClassifier]:
    """Return an OllamaClassifier when STRATA_LLM_BACKEND (or CNO_LLM_BACKEND) is
    'local'/'ollama' AND the local model is reachable; otherwise None so nodes
    keep using heuristics. Never raises — safe to call at import time.
    """
    backend = (os.environ.get("STRATA_LLM_BACKEND")
               or os.environ.get("CNO_LLM_BACKEND") or "").strip().lower()
    if backend not in ("local", "ollama"):
        return None
    if not _OLLAMA_IMPORTED:
        return None
    try:
        clf = OllamaClassifier()
        return clf if clf.available() else None
    except Exception:
        return None
