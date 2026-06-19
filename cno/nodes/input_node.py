"""
Input Node — detects modality, tone, type of incoming request, plus a
confidence score on each axis.

Per Sentinel Forge System Core Directive:
  [Input Node] Detects modality, tone, and type of request (question, command, abstract).

Heuristics use scored lexicons + structural signals, not just regex one-shots.
Optional LLMClassifier backend can override.

Glyph: 📥 (Input — intake)
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import re

from ..llm import LLMClassifier


class Modality(str, Enum):
    TEXT     = "text"
    VOICE    = "voice"      # voice-to-text — proprietor's primary input mode
    IMAGE    = "image"
    VIDEO    = "video"
    DOCUMENT = "document"
    CODE     = "code"
    MIXED    = "mixed"


class RequestType(str, Enum):
    QUESTION    = "question"
    COMMAND     = "command"
    ABSTRACT    = "abstract"
    DECLARATIVE = "declarative"


class Tone(str, Enum):
    NEUTRAL    = "neutral"
    URGENT     = "urgent"
    REFLECTIVE = "reflective"
    DIRECTIVE  = "directive"
    PLAYFUL    = "playful"


@dataclass(frozen=True)
class InputClassification:
    modality:           Modality
    request_type:       RequestType
    tone:               Tone
    raw_length:         int
    modality_confidence:     float = 0.0
    request_type_confidence: float = 0.0
    tone_confidence:         float = 0.0
    glyph_tag:          str   = "📥"


# --- Lexicons ---

_TONE_LEXICON: dict[Tone, tuple[str, ...]] = {
    Tone.URGENT: (
        "urgent", "asap", "immediately", "right now", "right away",
        "stat", "quickly", "as fast as", "time-critical", "emergency",
    ),
    Tone.REFLECTIVE: (
        "reflect", "consider", "think about", "ponder", "wonder",
        "feel", "contemplate", "muse", "introspect", "philosophical",
        "deeper meaning", "what does it mean",
    ),
    Tone.PLAYFUL: (
        "fun", "cool", "awesome", "haha", "lol", "joke",
        "silly", "coconut", "🥥",
    ),
    Tone.DIRECTIVE: (
        "must", "should", "always", "never", "make sure",
        "do this", "begin",
    ),
}

_QUESTION_PREFIX = re.compile(
    r"^\s*(what|why|how|when|where|who|whose|which|"
    r"can|could|should|would|will|is|are|do|does|did|may|might)\b",
    re.IGNORECASE,
)

_CMD_VERBS = re.compile(
    r"^\s*(do|build|create|make|run|start|stop|kill|fix|review|read|"
    r"write|update|delete|sample|verify|check|deploy|push|pull|commit|"
    r"refactor|test|generate|draft|send|open|close|please)\b",
    re.IGNORECASE,
)

_ABSTRACT_HINTS = (
    "imagine", "what if", "consider the", "philosophical",
    "in theory", "hypothetically", "suppose",
)

_HEDGE_HINTS = (
    "perhaps", "maybe", "i think", "i believe", "kind of",
    "sort of", "i guess",
)


def _hits(text: str, terms: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for t in terms if t in lower)


class InputNode:
    """Classifies an incoming payload into modality + type + tone with confidence."""

    def __init__(self, llm: Optional[LLMClassifier] = None):
        self.llm = llm

    def classify(self, raw: str, modality_hint: Modality | None = None) -> InputClassification:
        if self.llm is not None:
            try:
                # Enum-aware schema hint: tell the backend the EXACT allowed values,
                # otherwise it invents out-of-enum words and we fall back needlessly.
                out = self.llm.classify(
                    raw,
                    '{"modality": one of '
                    '["text","voice","image","video","document","code","mixed"], '
                    '"request_type": one of '
                    '["question","command","abstract","declarative"], '
                    '"tone": one of '
                    '["neutral","urgent","reflective","directive","playful"]}',
                )
                return InputClassification(
                    modality     = Modality(out.get("modality", "text")),
                    request_type = RequestType(out.get("request_type", "declarative")),
                    tone         = Tone(out.get("tone", "neutral")),
                    raw_length   = len(raw),
                    modality_confidence     = float(out.get("modality_confidence", 0.9)),
                    request_type_confidence = float(out.get("request_type_confidence", 0.9)),
                    tone_confidence         = float(out.get("tone_confidence", 0.9)),
                )
            except Exception:
                pass  # fall through to heuristics

        modality, m_conf = (modality_hint, 1.0) if modality_hint else self._guess_modality(raw)
        request_type, rt_conf = self._guess_request_type(raw)
        tone, t_conf = self._guess_tone(raw)
        return InputClassification(
            modality=modality, request_type=request_type, tone=tone,
            raw_length=len(raw),
            modality_confidence=m_conf,
            request_type_confidence=rt_conf,
            tone_confidence=t_conf,
        )

    def _guess_modality(self, raw: str) -> tuple[Modality, float]:
        lower = raw.lower()
        if "://" in raw and any(ext in lower for ext in (".mp4", ".mov", ".webm")):
            return Modality.VIDEO, 0.95
        if "://" in raw and any(ext in lower for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return Modality.IMAGE, 0.95
        if "://" in raw and any(ext in lower for ext in (".docx", ".pdf", ".txt", ".md")):
            return Modality.DOCUMENT, 0.95
        if raw.startswith(("```", "def ", "class ", "function ", "import ", "from ")):
            return Modality.CODE, 0.9
        # Voice-to-text signature: trailing slash, "./" patterns, many slashes/periods
        voice_signals = sum([
            raw.rstrip().endswith("/"),
            "./" in raw, "/." in raw,
            raw.count("/") >= 2, raw.count(".") > 5,
        ])
        if voice_signals >= 2:
            return Modality.VOICE, 0.85
        if voice_signals == 1:
            return Modality.VOICE, 0.6
        return Modality.TEXT, 0.7

    def _guess_request_type(self, raw: str) -> tuple[RequestType, float]:
        if any(h in raw.lower() for h in _ABSTRACT_HINTS):
            return RequestType.ABSTRACT, 0.85
        cmd = bool(_CMD_VERBS.search(raw))
        q   = bool(_QUESTION_PREFIX.search(raw)) or "?" in raw
        if cmd and q:
            # ambiguous — lean on which signal is stronger
            return (RequestType.QUESTION, 0.55) if "?" in raw else (RequestType.COMMAND, 0.55)
        if cmd:
            return RequestType.COMMAND, 0.85
        if q:
            return RequestType.QUESTION, 0.85
        return RequestType.DECLARATIVE, 0.6

    def _guess_tone(self, raw: str) -> tuple[Tone, float]:
        scores: dict[Tone, int] = {t: _hits(raw, terms) for t, terms in _TONE_LEXICON.items()}
        scores[Tone.NEUTRAL] = 0  # neutral has no positive lexicon
        best = max(scores, key=lambda t: scores[t])
        if scores[best] == 0:
            return Tone.NEUTRAL, 0.55
        # Confidence rises with hits and gap to runner-up
        sorted_scores = sorted(scores.values(), reverse=True)
        gap = sorted_scores[0] - (sorted_scores[1] if len(sorted_scores) > 1 else 0)
        conf = min(0.95, 0.5 + 0.15 * scores[best] + 0.05 * gap)
        return best, conf
