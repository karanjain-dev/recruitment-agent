"""Bounded, observable OpenAI calls. Models propose; the engine owns decisions.

Each public method makes at most one provider request. A turn shares one gateway
and CallBudget; the orchestrator alone decides whether a failed reply is retried.
An injected httpx.AsyncClient supports deterministic, network-free tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from app.engine import FLAG_ROUTES


Emit = Callable[..., Awaitable[None]]
_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_ENDPOINT = "https://api.openai.com/v1/chat/completions"
_DEFAULT_MODEL = "gpt-4.1-mini"
_FLAGS = ["none", *FLAG_ROUTES]


class ModelError(RuntimeError):
    """A safe public error. Never includes a provider body or authorization data."""

    def __init__(self, message: str, *, code: str = "model_error", retryable: bool = False,
                 status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.retryable = retryable
        self.status_code = status_code


@dataclass
class CallBudget:
    limit: int = 6
    used: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 6 or not 0 <= self.used <= self.limit:
            raise ValueError("A turn budget must allow between one and six calls.")

    def consume(self) -> int:
        if self.used >= self.limit:
            raise ModelError("This turn reached its model call limit.", code="call_budget_exceeded")
        self.used += 1
        return self.used


def model_ready() -> bool:
    """Configuration readiness only; provider connectivity is checked on a call."""
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STRING = {"type": "string"}
_NULLABLE_STRING = {"type": ["string", "null"]}
_BOOLEAN = {"type": "boolean"}
_ANSWER = _object({
    "criterion": _STRING,
    "event": {"type": "string", "enum": ["answer", "volunteered", "correction", "late_answer"]},
    "status": {"type": "string", "enum": ["complete", "partial", "unclear", "conditional", "declined"]},
    "value": _STRING,
    "quote": _STRING,
    "yes_no": {"type": ["string", "null"], "enum": ["yes", "no", None]},
    "implied": _BOOLEAN,
    "condition": _NULLABLE_STRING,
    "missing_part": _NULLABLE_STRING,
})
UNDERSTAND_SCHEMA = _object({
    "answers": {"type": "array", "items": _ANSWER},
    "candidate_questions": {"type": "array", "items": _object({
        "text": _STRING,
        "type": {"type": "string", "enum": ["job", "process", "clarify", "outcome", "assessment_hint"]},
        "fact_key": _NULLABLE_STRING,
    })},
    "flag": {"type": "string", "enum": _FLAGS},
    "stop": _BOOLEAN,
    "callback": _object({"requested": _BOOLEAN, "time": _NULLABLE_STRING}),
})
SPEAK_SCHEMA = _object({"acknowledgement": _STRING, "answer_text": _STRING})
ROLEPLAY_CLASSIFY_SCHEMA = _object({
    "event": {"type": "string", "enum": ["roleplay_reply", "roleplay_break", "stop"]},
    "flag": {"type": "string", "enum": _FLAGS},
})
ROLEPLAY_REPLY_SCHEMA = _object({"customer_line": _STRING})


def _validate(value: Any, schema: dict[str, Any]) -> bool:
    """Verify the exact schema locally, including types, keys and enum values."""
    expected = schema["type"]
    choices = expected if isinstance(expected, list) else [expected]
    if value is None:
        return "null" in choices and ("enum" not in schema or value in schema["enum"])
    actual = ("boolean" if isinstance(value, bool) else "string" if isinstance(value, str)
              else "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "unknown")
    if actual not in choices or ("enum" in schema and value not in schema["enum"]):
        return False
    if actual == "object":
        properties = schema["properties"]
        return set(value) == set(properties) and all(_validate(value[k], v) for k, v in properties.items())
    if actual == "array":
        return all(_validate(item, schema["items"]) for item in value)
    return True


def _understand_context(context: dict[str, Any]) -> dict[str, Any]:
    """Structural privacy boundary: never forward extra state or saved answers."""
    current = context.get("current_criterion")
    return {
        "current_criterion": {key: current.get(key) for key in
                              ("id", "name", "complete_when", "examples", "is_yes_no")}
        if isinstance(current, dict) else None,
        "confirming": context.get("confirming") is True,
        "allowed_missing_parts": [x for x in context.get("allowed_missing_parts", []) if isinstance(x, str)],
        "other_criteria": [{key: row.get(key) for key in ("id", "about", "status")}
                           for row in context.get("other_criteria", []) if isinstance(row, dict)],
        "hints": [x for x in context.get("hints", []) if isinstance(x, str)],
        "job_fact_topics": [{key: row.get(key) for key in ("key", "topic")}
                            for row in context.get("job_fact_topics", []) if isinstance(row, dict)],
        "candidate_messages": [x for x in context.get("candidate_messages", []) if isinstance(x, str)][-6:],
        "latest_message": context.get("latest_message", ""),
    }


class ModelGateway:
    def __init__(self, budget: CallBudget | None = None, *, client: httpx.AsyncClient | None = None,
                 api_key: str | None = None, model: str | None = None, timeout: float = 25.0,
                 understand_model: str | None = None, speak_model: str | None = None,
                 temperature: float | None = None, prompts: dict[str, str] | None = None) -> None:
        self.budget = budget if budget is not None else CallBudget()
        self.client = client
        self.api_key = (api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")).strip()
        self.model = (model or os.environ.get("OPENAI_MODEL") or _DEFAULT_MODEL).strip()
        self.models = {"understand": understand_model or self.model,
                       "speak": speak_model or self.model,
                       "roleplay_classify": understand_model or self.model,
                       "roleplay_reply": speak_model or self.model}
        self.temperature = temperature
        self.prompts = dict(prompts or {})
        if temperature is not None and (isinstance(temperature, bool) or not 0 <= temperature <= 2):
            raise ValueError("Model temperature must be between zero and two.")
        if any(not isinstance(value, str) or not value.strip() for value in self.prompts.values()):
            raise ValueError("Prompt overrides must contain nonempty text.")
        if not 0 < timeout <= 90:
            raise ValueError("Model timeout must be positive and no greater than 90 seconds.")
        self.timeout_seconds = timeout
        self.timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))

    async def understand(self, context: dict[str, Any], emit: Emit) -> dict[str, Any]:
        return await self._call("understand", _understand_context(context), UNDERSTAND_SCHEMA, emit, 1800)

    async def speak(self, saved: list[Any], facts: list[Any], unknowns: list[Any], emit: Emit) -> dict[str, Any]:
        data = {"saved": saved, "facts": facts, "unknown_questions": unknowns}
        return await self._call("speak", data, SPEAK_SCHEMA, emit, 650)

    async def classify_roleplay(self, candidate: str, persona: Any, last_line: str, emit: Emit) -> dict[str, Any]:
        data = {"customer_persona": persona, "last_customer_line": last_line, "candidate_message": candidate}
        return await self._call("roleplay_classify", data, ROLEPLAY_CLASSIFY_SCHEMA, emit, 200)

    async def roleplay_reply(self, candidate: str, persona: Any, last_line: str, emit: Emit) -> dict[str, Any]:
        data = {"customer_persona": persona, "last_customer_line": last_line, "candidate_message": candidate}
        return await self._call("roleplay_reply", data, ROLEPLAY_REPLY_SCHEMA, emit, 250)

    async def _request(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.client is not None:
            return await self.client.post(_ENDPOINT, headers=headers, json=payload, timeout=self.timeout,
                                          follow_redirects=False)
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            return await client.post(_ENDPOINT, headers=headers, json=payload)

    async def _call(self, name: str, data: dict[str, Any], schema: dict[str, Any], emit: Emit,
                    max_tokens: int) -> dict[str, Any]:
        started = time.perf_counter()
        selected_model = self.models.get(name, self.model)
        try:
            call_number = self.budget.consume()
        except ModelError as exc:
            await emit("model", name, "failed", input={"model": selected_model, "limit": self.budget.limit},
                       duration_ms=0, error={"code": exc.code, "message": str(exc)})
            raise

        messages = [
            {"role": "system", "content": self.prompts.get(name) or (_PROMPTS / f"{name}.md").read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)},
        ]
        response_format = {"type": "json_schema", "json_schema": {
            "name": f"screening_{name}", "strict": True, "schema": schema,
        }}
        payload = {"model": selected_model, "messages": messages, "response_format": response_format,
                   "max_completion_tokens": max_tokens, "store": False}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        await emit("model", name, "started", input={"model": selected_model, "call_number": call_number,
                   "temperature": self.temperature,
                   "messages": messages, "response_format": response_format})
        # Keep the chosen model on failures too: otherwise split understand/speak
        # configurations lose their identity when a provider call times out.
        model_output = {"model": selected_model}
        try:
            if not self.api_key:
                raise ModelError("The interview model is not configured. Please contact the app owner.",
                                 code="model_not_configured")
            response = await asyncio.wait_for(self._request(payload), timeout=self.timeout_seconds)
            status = response.status_code
            if status >= 300:
                if status in (401, 403):
                    exc = ModelError("The model provider could not authorize this app.", code="provider_auth", status_code=status)
                elif status == 429:
                    exc = ModelError("The model provider is temporarily at its request limit. Please retry shortly.",
                                     code="provider_rate_limit", retryable=True, status_code=status)
                elif status >= 500 or status in (408, 409):
                    exc = ModelError("The model provider is temporarily unavailable. Please retry.",
                                     code="provider_unavailable", retryable=True, status_code=status)
                else:
                    exc = ModelError("The model provider could not process this request.", code="provider_request", status_code=status)
                raise exc
            try:
                body = response.json()
                choice = body["choices"][0]
                message = choice["message"]
                model_output = {
                    "model": body.get("model", selected_model),
                    "response_id": body.get("id"),
                    "provider_request_id": response.headers.get("x-request-id"),
                    "finish_reason": choice.get("finish_reason"),
                    "response": message.get("content"),
                    "raw_text": message.get("content"),
                    "usage": {key: value for key, value in (body.get("usage") or {}).items()
                              if key in ("prompt_tokens", "completion_tokens", "total_tokens") and isinstance(value, int)}
                    if isinstance(body.get("usage"), dict) else {},
                }
                if message.get("refusal"):
                    raise ModelError("The model could not process this message. Please rephrase it.", code="model_refusal")
                if choice.get("finish_reason") != "stop":
                    raise ModelError("The model returned an incomplete response. Please retry.", code="model_incomplete", retryable=True)
                raw_content = message["content"]
                if not isinstance(raw_content, str) or len(raw_content) > 100_000:
                    raise ValueError("Unexpected response content")
                result = json.loads(raw_content)
                model_output["parsed_json"] = result
                if not _validate(result, schema):
                    raise ValueError("Response did not match its schema")
            except ModelError:
                raise
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                raise ModelError("The model returned an invalid response. Please retry.", code="model_invalid_response", retryable=True) from exc

            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            safe_usage = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                          if isinstance(usage.get(key), int)}
            await emit("model", name, "completed", duration_ms=round((time.perf_counter() - started) * 1000),
                       output={"model": body.get("model", selected_model), "response_id": body.get("id"),
                               "provider_request_id": response.headers.get("x-request-id"),
                               "usage": safe_usage, "raw_text": raw_content, "response": result})
            return result
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            error = ModelError("The model took too long to respond. Please retry.", code="model_timeout", retryable=True)
            await self._failed(name, started, error, emit, output=model_output)
            raise error from exc
        except httpx.HTTPError as exc:
            error = ModelError("The model provider could not be reached. Please retry.", code="model_network", retryable=True)
            await self._failed(name, started, error, emit, output=model_output)
            raise error from exc
        except ModelError as exc:
            await self._failed(name, started, exc, emit, output=model_output)
            raise
        except Exception:
            # Retain a terminal model event for an unexpected client failure,
            # without logging a possibly credential-bearing exception string or
            # changing how the existing API handles the original exception.
            error = ModelError("The model request could not finish.", code="model_unexpected_error")
            await self._failed(name, started, error, emit, output=model_output)
            raise

    @staticmethod
    async def _failed(name: str, started: float, error: ModelError, emit: Emit,
                      output: dict[str, Any] | None = None) -> None:
        await emit("model", name, "failed", duration_ms=round((time.perf_counter() - started) * 1000),
                   output=output,
                   error={"code": error.code, "message": str(error), "retryable": error.retryable,
                          "provider_status": error.status_code})
