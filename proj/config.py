"""Tiny config loader - one function, no defaults duplicated here.

Every tunable lives in config.yaml (see that file's comments for what each
one controls and, once chosen, how). Loading fails loudly on a missing key
rather than silently falling back, so a typo in config.yaml surfaces
immediately instead of quietly using a stale default.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)
