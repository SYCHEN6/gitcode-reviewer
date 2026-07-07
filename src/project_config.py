"""Per-project configuration loader.

Config files live in project_configs/{owner}__{repo}.yaml
(the "/" in project_id is replaced with "__").
Falls back to project_configs/_default.yaml if no project-specific file exists.
"""

import logging
import os
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "project_configs")

_DEFAULTS: dict[str, Any] = {
    "agents":       [],
    "max_findings": 30,
    "min_severity": "LOW",
    "max_files":    0,
}


def _project_id_to_filename(project_id: str) -> str:
    return project_id.replace("/", "__") + ".yaml"


def _load_yaml(path: str) -> dict:
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data
    except ImportError:
        logger.debug("PyYAML not installed — using default config only")
        return {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("Failed to load config %s: %s", path, e)
        return {}


@lru_cache(maxsize=256)
def load_project_config(project_id: str) -> dict[str, Any]:
    """Return merged config for project_id (project-specific overrides defaults)."""
    default_path = os.path.join(_CONFIG_DIR, "_default.yaml")
    project_path = os.path.join(_CONFIG_DIR, _project_id_to_filename(project_id))

    cfg = dict(_DEFAULTS)
    cfg.update(_load_yaml(default_path))
    cfg.update(_load_yaml(project_path))
    return cfg


def get_enabled_agents(project_id: str) -> list[str]:
    """Return the agent whitelist for project_id, or [] meaning 'use rule engine'."""
    return list(load_project_config(project_id).get("agents") or [])


def get_max_findings(project_id: str) -> int:
    return int(load_project_config(project_id).get("max_findings") or 0)


def get_min_severity(project_id: str) -> str:
    return str(load_project_config(project_id).get("min_severity") or "LOW").upper()


def get_max_files(project_id: str) -> int:
    return int(load_project_config(project_id).get("max_files") or 0)


def filter_findings_by_config(findings: list[dict], project_id: str) -> list[dict]:
    """Apply per-project min_severity and max_findings filters."""
    min_sev = get_min_severity(project_id)
    min_level = _SEVERITY_ORDER.get(min_sev, 1)

    filtered = [
        f for f in findings
        if _SEVERITY_ORDER.get(f.get("severity", "LOW"), 1) >= min_level
    ]
    max_f = get_max_findings(project_id)
    if max_f > 0:
        filtered = filtered[:max_f]
    return filtered
