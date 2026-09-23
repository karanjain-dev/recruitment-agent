"""Versioned model configurations; no process-global model or prompt mutation."""
from __future__ import annotations

import copy
import json
from pathlib import Path

CONFIG_DIR = Path(__file__).parent
PROJECT_ROOT = CONFIG_DIR.parent.parent
_UNSET = object()


def list_model_configs() -> list[dict]:
    configurations = json.loads((CONFIG_DIR / "models.yaml").read_text())["models"]
    return [dict(copy.deepcopy(value), name=name, understand_model=value["model"], speak_model=value["model"])
            for name, value in configurations.items()]


def resolve_model_config(name: str, *, understand_model=None, speak_model=None, temperature=_UNSET) -> dict:
    options = {row["name"]: row for row in list_model_configs()}
    if name not in options:
        raise ValueError("Unknown model configuration: " + str(name))
    config = options[name]
    for key, value in (("understand_model", understand_model), ("speak_model", speak_model)):
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > 150:
                raise ValueError(key + " must be a non-empty model ID")
            config[key] = value.strip()
    if temperature is not _UNSET:
        if temperature is not None and (not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not 0 <= temperature <= 2):
            raise ValueError("temperature must be between 0 and 2")
        config["temperature"] = temperature
    return config


def get_grader_config() -> dict:
    config = json.loads((CONFIG_DIR / "grader.yaml").read_text())
    if config.get("temperature") != 0:
        raise ValueError("The fixed grader configuration must have temperature zero")
    return config


def get_default_prompts() -> dict[str, str]:
    return {name: (PROJECT_ROOT / "prompts" / (name + ".md")).read_text() for name in ("understand", "speak")}


def resolve_prompts(overrides=None) -> dict[str, str]:
    prompts = get_default_prompts()
    if overrides is None:
        return prompts
    if not isinstance(overrides, dict) or set(overrides) - set(prompts):
        raise ValueError("prompts accepts only understand and speak text")
    for key, value in overrides.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 100000:
            raise ValueError(key + " prompt must contain 1 to 100,000 characters")
        prompts[key] = value
    return prompts
