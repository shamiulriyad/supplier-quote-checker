import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@lru_cache
def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["rules"] = {k: Decimal(str(v)) for k, v in cfg["rules"].items()}
    cfg["llm"]["model"] = os.environ.get("OPENAI_MODEL", cfg["llm"]["model"])
    for key in ("purchase_request", "documents_dir", "cache_dir"):
        cfg[key] = str(ROOT / cfg[key])
    return cfg
