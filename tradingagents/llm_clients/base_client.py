from abc import ABC, abstractmethod
from typing import Any, Optional
import re
import warnings


_REASONING_TAGS = ("think", "reasoning")
_FINAL_MARKERS = (
    "[FINAL_ANALYST_REPORT]",
    "FINAL TRANSACTION PROPOSAL:",
    "FINAL RATING:",
    "Rating:",
    "**Rating**:",
)


def strip_reasoning_traces(text: str) -> str:
    """Remove raw model scratchpad/reasoning traces from provider text output."""
    if not isinstance(text, str) or not text:
        return text or ""

    cleaned = text
    tag_names = "|".join(_REASONING_TAGS)

    # Remove complete hidden-reasoning blocks first.
    cleaned = re.sub(
        rf"(?is)<(?:{tag_names})\b[^>]*>.*?</(?:{tag_names})>",
        "",
        cleaned,
    )

    # Some local models leak text before a closing tag, e.g.
    # "internal draft...</think>\n[FINAL_ANALYST_REPORT]...".
    cleaned = re.sub(rf"(?is)^.*?</(?:{tag_names})>\s*", "", cleaned)

    # If an opening tag is left unclosed but a known final-output marker follows,
    # keep the final section. Otherwise drop the dangling scratchpad.
    for tag in _REASONING_TAGS:
        open_match = re.search(rf"(?is)<{tag}\b[^>]*>", cleaned)
        while open_match:
            marker_positions = [
                pos
                for marker in _FINAL_MARKERS
                if (pos := cleaned.find(marker, open_match.end())) != -1
            ]
            if marker_positions:
                marker_pos = min(marker_positions)
                cleaned = cleaned[: open_match.start()] + cleaned[marker_pos:]
            else:
                cleaned = cleaned[: open_match.start()]
            open_match = re.search(rf"(?is)<{tag}\b[^>]*>", cleaned)

    cleaned = re.sub(rf"(?is)</?(?:{tag_names})\b[^>]*>", "", cleaned)

    # Collapse excessive blank space left by removed blocks while preserving
    # normal report paragraph structure.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def normalize_content(response):
    """Normalize LLM response content to a plain string.

    Multiple providers (OpenAI Responses API, Google Gemini 3) return content
    as a list of typed blocks, e.g. [{'type': 'reasoning', ...}, {'type': 'text', 'text': '...'}].
    Downstream agents expect response.content to be a string. This extracts
    and joins the text blocks, discarding reasoning/metadata blocks.
    """
    content = response.content
    if isinstance(content, list):
        texts = [
            item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
            else item if isinstance(item, str) else ""
            for item in content
        ]
        response.content = "\n".join(t for t in texts if t)
    if isinstance(response.content, str):
        response.content = strip_reasoning_traces(response.content)
    return response


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients."""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        self.model = model
        self.base_url = base_url
        self.kwargs = kwargs

    def get_provider_name(self) -> str:
        """Return the provider name used in warning messages."""
        provider = getattr(self, "provider", None)
        if provider:
            return str(provider)
        return self.__class__.__name__.removesuffix("Client").lower()

    def warn_if_unknown_model(self) -> None:
        """Warn when the model is outside the known list for the provider."""
        if self.validate_model():
            return

        warnings.warn(
            (
                f"Model '{self.model}' is not in the known model list for "
                f"provider '{self.get_provider_name()}'. Continuing anyway."
            ),
            RuntimeWarning,
            stacklevel=2,
        )

    @abstractmethod
    def get_llm(self) -> Any:
        """Return the configured LLM instance."""
        pass

    @abstractmethod
    def validate_model(self) -> bool:
        """Validate that the model is supported by this client."""
        pass
