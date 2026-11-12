"""Paths, config loading and .env support."""
from __future__ import annotations

import copy
import os
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROFILE_DIR = DATA / "fb_profile"  # legacy single-account profile
PROFILES_DIR = DATA / "profiles"   # multi-agent profiles: data/profiles/<agent_id>/
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


def get_profile_dir(agent_id: str | None = None) -> pathlib.Path:
    """Return the profile directory for a specific agent or default."""
    if not agent_id or agent_id == "default":
        # Check if legacy single profile exists first, else use profiles/default
        if PROFILE_DIR.exists() and not (PROFILES_DIR / "default").exists():
            return PROFILE_DIR
        return PROFILES_DIR / "default"
    return PROFILES_DIR / agent_id


def get_agents(cfg: dict) -> dict[str, dict]:
    """Return the mapping of agent_id -> agent configuration dict.
    
    If 'agents' is defined in config.yaml, returns all configured agents.
    Otherwise returns a single {'default': <root_config>} agent for full backward compatibility.
    """
    configured_agents = cfg.get("agents")
    if isinstance(configured_agents, dict) and configured_agents:
        result = {}
        for agent_id, agent_cfg in configured_agents.items():
            result[agent_id] = get_agent_config(cfg, agent_id)
        return result
    return {"default": get_agent_config(cfg, "default")}


def get_agent_config(cfg: dict, agent_id: str) -> dict:
    """Merge agent-specific settings with global configuration."""
    base = copy.deepcopy(cfg)
    agent_overrides = (cfg.get("agents") or {}).get(agent_id, {})
    
    agent_merged = {
        "agent_id": agent_id,
        "name": agent_overrides.get("name", agent_id),
        "persona": agent_overrides.get("persona", base.get("persona")),
        "destination": base.get("destination", ""),
        "groups": base.get("groups", []),
        "browser_channel": agent_overrides.get("browser_channel", base.get("browser_channel", "chrome")),
        "knowledge_file": agent_overrides.get("knowledge_file", "knowledge.md"),
        "scan": _merge(base.get("scan", {}), agent_overrides.get("scan", {})),
        "post": _merge(base.get("post", {}), agent_overrides.get("post", {})),
        "llm": _merge(base.get("llm", {}), agent_overrides.get("llm", {})),
        "collaboration": _merge({
            "min_delay_after_primary_mins": 15,
            "allow_like": True,
            "allow_reply": True,
        }, agent_overrides.get("collaboration", {})),
    }
    return agent_merged


def load_knowledge(knowledge_file: str | pathlib.Path | None = None) -> str:
    path = KNOWLEDGE_PATH
    if knowledge_file:
        p = pathlib.Path(knowledge_file)
        path = p if p.is_absolute() else (ROOT / p)
    if not path.exists():
        if path != KNOWLEDGE_PATH and KNOWLEDGE_PATH.exists():
            return KNOWLEDGE_PATH.read_text().strip() or "(no local notes yet)"
        return "(no local notes yet)"
    text = path.read_text().strip()
    return text or "(no local notes yet)"
