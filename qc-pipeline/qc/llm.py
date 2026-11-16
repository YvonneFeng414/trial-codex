"""OpenAI calls with Pydantic structured output, embeddings, and the _cost.tsv log (D6)."""

from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from qc import QC_ROOT
from qc.config import ConfigError, check_model
from qc.text import sha

T = TypeVar("T", bound=BaseModel)
COST_FIELDS = ("time", "doc_id", "stage", "model", "prompt_sha", "input_tokens", "output_tokens", "n_items")
ENV_FILE = QC_ROOT / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    """Read KEY=VALUE lines from qc-pipeline/.env; variables already set in the shell win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require_api_key() -> str:
    """The API key: OPENAI_API_KEY if set, else API_KEY (the name qc-pipeline/.env uses)."""
    load_env()
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("API_KEY")
    if not key:
        raise ConfigError(
            f"no API key: set API_KEY in {ENV_FILE} (or OPENAI_API_KEY in the shell), or run with --backend stub"
        )
    return key


# OpenAI caps an embeddings request at 300k tokens and each input at 8,192. Garbled or
# unspaced text can cost one token per character, so batches are sized in characters.
EMBED_MAX_INPUT_CHARS = 8000
EMBED_MAX_BATCH_CHARS = 250_000
EMBED_MAX_BATCH_INPUTS = 256


def embedding_batches(texts: list[str]) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for text in texts:
        text = text[:EMBED_MAX_INPUT_CHARS] or " "
        if current and (size + len(text) > EMBED_MAX_BATCH_CHARS or len(current) >= EMBED_MAX_BATCH_INPUTS):
            batches.append(current)
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        batches.append(current)
    return batches


class CostLog:
    def __init__(self, path: Path | None):
        self.path = path
        self.rows: list[dict[str, Any]] = []

    def add(self, **row: Any) -> None:
        row.setdefault("time", time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.rows.append(row)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.is_file()
        with open(self.path, "a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=COST_FIELDS, delimiter="\t", extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerow(row)

    def total(self, key: str) -> int:
        return sum(int(row.get(key) or 0) for row in self.rows)


class OpenAIBackend:
    """The only path to a model. Stub runs pass `backend=None` to each stage instead."""

    def __init__(self, config: dict[str, Any], cost: CostLog):
        from openai import OpenAI

        api_key = require_api_key()
        self.config = config
        self.cost = cost
        self.no_temperature: set[str] = set()
        # .env names the endpoint OPENAI_API_URL; the SDK itself reads OPENAI_BASE_URL.
        base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_URL") or None
        self.client = OpenAI(api_key=api_key, base_url=base_url, max_retries=5)

    def parse(
        self,
        *,
        role: str,
        instructions: str,
        payload: str,
        schema: type[T],
        doc_id: str,
        stage: str,
        n_items: int,
    ) -> T:
        model = check_model(self.config, role)
        from openai import BadRequestError

        options: dict[str, Any] = {"max_output_tokens": self.config["models"]["max_output_tokens"]}
        if self.config["models"].get("temperature") is not None and model not in self.no_temperature:
            options["temperature"] = self.config["models"]["temperature"]
        try:
            response = self.client.responses.parse(
                model=model, instructions=instructions, input=payload, text_format=schema, **options
            )
        except BadRequestError as exc:
            # Reasoning models reject `temperature`; drop it for this model and say so once.
            if "temperature" not in options or "temperature" not in str(exc).lower():
                raise
            self.no_temperature.add(model)
            print(f"warning: {model} rejects temperature; calling it without one", file=sys.stderr)
            options.pop("temperature")
            response = self.client.responses.parse(
                model=model, instructions=instructions, input=payload, text_format=schema, **options
            )
        usage = getattr(response, "usage", None)
        self.cost.add(
            doc_id=doc_id,
            stage=stage,
            model=model,
            prompt_sha=sha(instructions),
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            n_items=n_items,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError(f"{stage}: model returned no parsable output")
        return parsed

    def embed(self, texts: list[str], doc_id: str, stage: str = "embed") -> list[list[float]]:
        model = self.config["models"]["embedding"]
        vectors: list[list[float]] = []
        tokens = 0
        for batch in embedding_batches(texts):
            response = self.client.embeddings.create(model=model, input=batch)
            vectors.extend(item.embedding for item in response.data)
            tokens += getattr(response.usage, "prompt_tokens", 0)
        self.cost.add(
            doc_id=doc_id, stage=stage, model=model, prompt_sha="", input_tokens=tokens,
            output_tokens=0, n_items=len(texts),
        )
        return vectors
