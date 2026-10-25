"""Load qc/config.yaml and enforce the judge-independence rule (D6)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from qc import QC_ROOT

DEFAULT_CONFIG = QC_ROOT / "config.yaml"


class ConfigError(ValueError):
    pass


def load(path: Path | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    for dotted, value in (overrides or {}).items():
        if value is None:
            continue
        node = config
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    return config


def check_model(config: dict[str, Any], role: str) -> str:
    """Return the model for `role`, refusing one the generator or reviewer uses."""
    models = config["models"]
    model = models.get(role) or (models.get("judge") if role == "atomizer" else None)
    if not model:
        raise ConfigError(
            f"models.{role} is unset in config.yaml; the team must choose a third model "
            "(implementation_plan.md, open question 1), or run with --backend stub"
        )
    if not is_independent(config, model) and not models.get("allow_non_independent_judge"):
        raise ConfigError(
            f"models.{role} = {model!r} is the generator or reviewer model; a model "
            "grading its own or its reviewer's output is not an independent check (D6). "
            "Set models.allow_non_independent_judge: true to override."
        )
    return model


def is_independent(config: dict[str, Any], model: str | None) -> bool:
    """False when `model` is one of the forbidden generator/reviewer models (prefix match)."""
    if not model:
        return True
    return not any(model.lower().startswith(b.lower()) for b in config["models"].get("forbidden", []))


def judge_independent(config: dict[str, Any]) -> bool:
    models = config["models"]
    return is_independent(config, models.get("judge")) and is_independent(config, models.get("atomizer"))


def snapshot(config: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(config)
