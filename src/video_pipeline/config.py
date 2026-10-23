from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path, sidecar: Path | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Configuration not found: {path}")
    config = yaml.safe_load(path.read_text()) or {}
    if sidecar and sidecar.is_file():
        config = _merge(config, yaml.safe_load(sidecar.read_text()) or {})
    config["input_dir"] = str(Path(config["input_dir"]).expanduser())
    return config
