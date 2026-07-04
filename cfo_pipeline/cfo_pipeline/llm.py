"""Thin wrappers around the OpenAI + Gemini SDKs.

Ported from ``insurance_pipeline.llm`` — kept by copy, not by import,
so each pipeline owns its own LLM surface (per CLAUDE.md).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, TypeVar, overload

import openai
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from openai import OpenAI
from pydantic import BaseModel, ValidationError

load_dotenv()

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
_DEFAULT_MAX_TOKENS = 1024
_MAX_RETRIES = 5
_BACKOFF_BASE_S = 1.5
# 429s get a bounded number of short retries; if they won't clear we
# treat the daily free-tier quota as spent (see _gemini_quota_exhausted)
# rather than sleeping ~1 min on every remaining lead.
_MAX_429_RETRIES = 3
_MAX_429_WAIT_S = 20.0

# Free-tier gemini-2.5-flash-lite caps generate_content at ~20 req/min.
# Enrichment fires one grounded Gemini call per lead, so a scaled fetch
# bursts straight through that ceiling without proactive pacing. Space
# calls to stay comfortably under the cap (default ~13/min); override
# via GEMINI_MIN_INTERVAL_S (e.g. "0" on a paid tier).
_GEMINI_MIN_INTERVAL_S = float(os.environ.get("GEMINI_MIN_INTERVAL_S", "4.5"))
_gemini_last_call_ts = 0.0

_RETRY_DELAY_RE = re.compile(r"retry in (\d+(?:\.\d+)?)", re.IGNORECASE)


def _throttle_gemini() -> None:
    """Short-circuit if the quota latch is set; otherwise block until at
    least ``_GEMINI_MIN_INTERVAL_S`` has elapsed since the previous
    Gemini call. Single-threaded pipeline, so a module global is
    sufficient."""
    if _gemini_quota_exhausted:
        raise GeminiQuotaExhausted("gemini quota exhausted earlier this run")
    global _gemini_last_call_ts
    if _GEMINI_MIN_INTERVAL_S <= 0:
        return
    now = time.monotonic()
    wait = _GEMINI_MIN_INTERVAL_S - (now - _gemini_last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _gemini_last_call_ts = time.monotonic()


def _genai_retry_delay(err: Exception) -> float | None:
    """Extract the server-suggested retry delay (seconds) from a 429
    message like 'Please retry in 44.03s'. The reactive backoff caps
    well below these hints, so honoring them is what actually lets a
    rate-limited call recover."""
    m = _RETRY_DELAY_RE.search(str(getattr(err, "message", "") or err))
    return float(m.group(1)) if m else None


class LLMError(RuntimeError):
    """Raised when a provider call exhausts retries or its response fails to validate."""


class GeminiQuotaExhausted(LLMError):
    """Raised when the free-tier daily Gemini quota looks spent. The
    enrichment loop catches this and stops making Gemini calls for the
    rest of the run — remaining leads defer to a later night — instead
    of sleeping through a ~60s retry hint on every one of them."""


# Latched once a 429 won't clear within the bounded retries. Every
# subsequent Gemini call short-circuits with GeminiQuotaExhausted so a
# quota wall stops the enrichment phase in seconds, not hours. Reset
# per-process (a fresh nightly run starts clean).
_gemini_quota_exhausted = False


def _reset_gemini_quota_latch() -> None:
    """Test hook — clear the module-level quota latch."""
    global _gemini_quota_exhausted
    _gemini_quota_exhausted = False


_openai_client: OpenAI | None = None
_gemini_client: genai.Client | None = None


def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise LLMError("OPENAI_API_KEY not set in environment")
        _openai_client = OpenAI(api_key=key)
    return _openai_client


def _get_gemini() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY not set in environment")
        _gemini_client = genai.Client(api_key=key)
    return _gemini_client


def _is_retryable_genai(err: genai_errors.APIError) -> bool:
    code = getattr(err, "code", None)
    if code == 429:
        return True
    if isinstance(code, int) and 500 <= code < 600:
        return True
    return False


def _with_retries(fn: Callable[[], Any], *, what: str) -> Any:
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return fn()
        except openai.RateLimitError as exc:
            last_exc = exc
            wait = _BACKOFF_BASE_S * (2**attempt)
            log.warning(
                "%s: openai rate limited (attempt %d/%d), sleeping %.1fs",
                what, attempt + 1, _MAX_RETRIES, wait,
            )
            time.sleep(wait)
        except openai.APIStatusError as exc:
            last_exc = exc
            status = exc.status_code
            if 500 <= status < 600 and attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF_BASE_S * (2**attempt)
                log.warning(
                    "%s: openai %d (attempt %d/%d), sleeping %.1fs",
                    what, status, attempt + 1, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            raise LLMError(f"{what}: openai non-retryable {status}") from exc
        except genai_errors.APIError as exc:
            last_exc = exc
            code = getattr(exc, "code", None)
            if code == 429:
                # Rate/quota limit. Retry a bounded number of times with
                # a capped wait; if it still won't clear, latch the quota
                # as spent and stop — don't sleep ~1 min per remaining
                # lead trying to drain an exhausted daily budget.
                if attempt < _MAX_429_RETRIES - 1:
                    hinted = _genai_retry_delay(exc)
                    wait = hinted if hinted is not None else _BACKOFF_BASE_S * (2**attempt)
                    wait = min(wait + 0.5, _MAX_429_WAIT_S)
                    log.warning(
                        "%s: gemini 429 (attempt %d/%d), sleeping %.1fs",
                        what, attempt + 1, _MAX_429_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue
                global _gemini_quota_exhausted
                _gemini_quota_exhausted = True
                log.warning(
                    "%s: gemini 429 persisted — latching quota as exhausted; "
                    "remaining enrichment defers to a later run", what,
                )
                raise GeminiQuotaExhausted(f"{what}: gemini quota exhausted") from exc
            if _is_retryable_genai(exc) and attempt < _MAX_RETRIES - 1:
                # 5xx transient server error — plain exponential backoff.
                wait = _BACKOFF_BASE_S * (2**attempt)
                log.warning(
                    "%s: gemini %s (attempt %d/%d), sleeping %.1fs",
                    what, code, attempt + 1, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            raise LLMError(f"{what}: gemini non-retryable {code}") from exc
    raise LLMError(f"{what}: exhausted {_MAX_RETRIES} retries") from last_exc


@overload
def call_openai(
    prompt: str,
    *,
    response_model: type[T],
    model: str = ...,
    max_tokens: int = ...,
) -> T: ...
@overload
def call_openai(
    prompt: str,
    *,
    response_model: None = None,
    model: str = ...,
    max_tokens: int = ...,
) -> str: ...
def call_openai(
    prompt: str,
    *,
    response_model: type[T] | None = None,
    model: str = _DEFAULT_OPENAI_MODEL,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> T | str:
    if response_model is not None:
        return _call_openai_structured(prompt, response_model, model, max_tokens)
    return _call_openai_text(prompt, model, max_tokens)


def _call_openai_text(prompt: str, model: str, max_tokens: int) -> str:
    client = _get_openai()

    def go() -> str:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.choices[0].message.content
        if text is None:
            log.debug("call_openai text: no content in response: %r", resp)
            raise LLMError("call_openai text: empty response")
        return text

    return _with_retries(go, what="call_openai text")


def _call_openai_structured(
    prompt: str, response_model: type[T], model: str, max_tokens: int
) -> T:
    client = _get_openai()

    def go() -> T:
        resp = client.beta.chat.completions.parse(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            response_format=response_model,
        )
        parsed = resp.choices[0].message.parsed
        if parsed is None:
            refusal = resp.choices[0].message.refusal
            log.debug(
                "call_openai structured: no parsed output for %s; refusal=%r",
                response_model.__name__, refusal,
            )
            raise LLMError(
                f"call_openai structured: {response_model.__name__} not returned "
                f"(refusal={refusal!r})"
            )
        return parsed

    return _with_retries(go, what="call_openai structured")


@overload
def call_gemini(prompt: str, *, response_model: type[T]) -> T: ...
@overload
def call_gemini(prompt: str, *, response_model: None = None) -> str: ...
def call_gemini(
    prompt: str, *, response_model: type[T] | None = None
) -> T | str:
    if response_model is not None:
        return _call_gemini_structured(prompt, response_model)
    return _call_gemini_text(prompt)


def _call_gemini_text(prompt: str) -> str:
    client = _get_gemini()
    config = genai_types.GenerateContentConfig(
        tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
    )

    def go() -> str:
        _throttle_gemini()
        resp = client.models.generate_content(
            model=_DEFAULT_GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
        text = resp.text
        if text is None:
            log.debug("call_gemini text: no text in response: %r", resp)
            raise LLMError("call_gemini text: empty response")
        if log.isEnabledFor(logging.DEBUG) and resp.candidates:
            gm = getattr(resp.candidates[0], "grounding_metadata", None)
            if gm is not None:
                log.debug("call_gemini grounding metadata: %r", gm)
        return text

    return _with_retries(go, what="call_gemini text")


def _call_gemini_structured(prompt: str, response_model: type[T]) -> T:
    client = _get_gemini()
    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=response_model,
    )

    def go() -> T:
        _throttle_gemini()
        resp = client.models.generate_content(
            model=_DEFAULT_GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
        parsed = resp.parsed
        if isinstance(parsed, response_model):
            return parsed
        raw = resp.text
        if raw is None:
            log.debug("call_gemini structured: no text/parsed in response: %r", resp)
            raise LLMError("call_gemini structured: empty response")
        try:
            return response_model.model_validate_json(raw)
        except ValidationError as exc:
            log.debug(
                "call_gemini structured: validation failed for %s, raw=%r",
                response_model.__name__, raw,
            )
            raise LLMError(
                f"call_gemini structured: {response_model.__name__} validation failed"
            ) from exc

    return _with_retries(go, what="call_gemini structured")
