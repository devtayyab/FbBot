"""Paths, config loading and .env support."""
from __future__ import annotations

import copy
import os
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROFILE_DIR = DATA / "fb_profile"  # logged-in browser profile (keep private)
DB_PATH = DATA / "bot.db"
DEBUG_DIR = DATA / "debug"
LOG_DIR = DATA / "logs"
CONFIG_PATH = ROOT / "config.yaml"
KNOWLEDGE_PATH = ROOT / "knowledge.md"
ENV_PATH = ROOT / ".env"

DEFAULTS: dict = {
    "groups": [],
    "destination": "",
    "persona": "a friendly local who has lived here for years and likes helping visitors",
    "browser_channel": "chrome",  # your installed Google Chrome; "chromium" = Playwright's bundled one
    "scan": {
        "scrolls": 8,
        "max_post_age_hours": 48,
        "headless": False,
    },
    "post": {
        "auto_post": False,
        "auto_post_min_confidence": "high",
        "max_per_run": 5,
        "max_per_day": 15,
        "min_delay_seconds": 90,
        "max_delay_seconds": 240,
        "comment_box_labels": [],
    },
    "llm": {
        "provider": "qwen",  # qwen | claude
        "model": "",  # empty = qwen-plus for qwen, claude-opus-5-5 for claude
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",  # qwen only
        "api_key_env": "DASHSCOPE_API_KEY",  # qwen only; claude reads ANTHROPIC_API_KEY
        "extra_body": {},  # qwen only; config.example.yaml sets enable_thinking: false for Alibaba Cloud
        "effort": "low",  # claude only
        "max_reply_words": 40,
    },
}
HISTORY_CSV = DATA / "comment_history.csv"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_env() -> None:
    """Load KEY=VALUE lines from .env without overriding real env vars.

    launchd does not read your shell profile, so the API key lives here.
    """
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(require_groups: bool = True, require_destination: bool | None = None) -> dict:
    load_env()
    if not CONFIG_PATH.exists():
        raise SystemExit(
            "config.yaml is missing. Run: cp config.example.yaml config.yaml, then fill in your group URL."
        )
    cfg = _merge(DEFAULTS, yaml.safe_load(CONFIG_PATH.read_text()) or {})
    if require_destination is None:
        require_destination = require_groups
    if require_groups:
        groups = cfg.get("groups") or []
        if not groups or any(not isinstance(g, str) or "YOUR_GROUP" in g for g in groups):
            raise SystemExit("Set at least one real group URL under 'groups' in config.yaml.")
    if require_destination:
        if not cfg.get("destination") or "YOUR_CITY" in cfg["destination"]:
            raise SystemExit("Set 'destination' in config.yaml (the place visitors are coming to).")
    confidence = str(cfg["post"]["auto_post_min_confidence"]).strip().lower()
    if confidence not in ("high", "medium", "low"):
        raise SystemExit("post.auto_post_min_confidence in config.yaml must be high, medium or low.")
    cfg["post"]["auto_post_min_confidence"] = confidence
    for path in (DATA, DEBUG_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
    return cfg


def load_knowledge() -> str:
    if not KNOWLEDGE_PATH.exists():
        return "(no local notes yet)"
    text = KNOWLEDGE_PATH.read_text().strip()
    return text or "(no local notes yet)"
