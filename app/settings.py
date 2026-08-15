"""Carga y fusión de la configuración de la aplicación.

Fuentes, de menor a mayor prioridad:
    1. config/settings.yaml           (archivo editable a mano)
    2. tabla settings_kv en SQLite    (cambios hechos desde la interfaz)

No hay ninguna llamada a servicios externos aquí: todo se lee del disco.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "app" / "web"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("TCG_DB_PATH", DATA_DIR / "tcg.db"))

_lock = threading.RLock()
_cache: Dict[str, Any] = {}


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Configuración principal
# ---------------------------------------------------------------------------
def load_settings(force: bool = False) -> Dict[str, Any]:
    """Devuelve settings.yaml fusionado con los overrides guardados en la BD."""
    with _lock:
        if not force and "settings" in _cache:
            return _cache["settings"]

        settings = _read_yaml(CONFIG_DIR / "settings.yaml")
        overrides = _load_db_overrides()
        merged = _deep_merge(settings, overrides)
        _cache["settings"] = merged
        return merged


def _load_db_overrides() -> Dict[str, Any]:
    """Lee los overrides de settings_kv. Tolera que la BD aún no exista."""
    try:
        from app.db.database import get_connection  # import diferido

        with get_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings_kv").fetchall()
    except Exception:
        return {}

    overrides: Dict[str, Any] = {}
    for row in rows:
        try:
            value = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            value = row["value"]
        _set_path(overrides, row["key"], value)
    return overrides


def _set_path(target: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):  # una clave hoja bloqueaba el camino
            return
    node[parts[-1]] = value


def get(dotted: str, default: Any = None) -> Any:
    """Acceso puntual: get('matching.auto_threshold')."""
    node: Any = load_settings()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def save_override(dotted: str, value: Any) -> None:
    from app.db.database import get_connection

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO settings_kv (key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = datetime('now')""",
            (dotted, json.dumps(value)),
        )
        conn.commit()
    invalidate()


def invalidate() -> None:
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Catálogos (normalización, tipos de producto, juegos, expansiones)
# ---------------------------------------------------------------------------
def load_normalization() -> Dict[str, Any]:
    with _lock:
        if "normalization" not in _cache:
            _cache["normalization"] = _read_yaml(CONFIG_DIR / "normalization.yaml")
        return _cache["normalization"]


def load_product_types() -> Dict[str, Any]:
    with _lock:
        if "product_types" not in _cache:
            _cache["product_types"] = _read_yaml(CONFIG_DIR / "product_types.yaml")
        return _cache["product_types"]


def load_games() -> List[Dict[str, Any]]:
    with _lock:
        if "games" not in _cache:
            data = _read_yaml(CONFIG_DIR / "games.yaml")
            _cache["games"] = data.get("games", [])
        return _cache["games"]


def load_sets() -> Dict[str, Dict[str, Any]]:
    """Devuelve {game_code: {game_name, sets: [...]}} leyendo config/sets/*.yaml."""
    with _lock:
        if "sets" in _cache:
            return _cache["sets"]

        catalogs: Dict[str, Dict[str, Any]] = {}
        for game in load_games():
            rel = game.get("sets_file")
            if not rel:
                continue
            data = _read_yaml(CONFIG_DIR / rel)
            if not data:
                continue
            catalogs[game["code"]] = {
                "game_name": data.get("game_name", game.get("name", game["code"])),
                "sets": data.get("sets", []),
            }
        _cache["sets"] = catalogs
        return catalogs


def load_store_configs() -> List[Dict[str, Any]]:
    """Lee todos los YAML de config/stores/. Cada uno define una tienda."""
    stores_dir = CONFIG_DIR / "stores"
    if not stores_dir.exists():
        return []
    configs: List[Dict[str, Any]] = []
    for path in sorted(stores_dir.glob("*.yaml")):
        data = _read_yaml(path)
        if not data:
            continue
        data["_config_file"] = str(path.relative_to(BASE_DIR)).replace("\\", "/")
        configs.append(data)
    return configs


def write_normalization(data: Dict[str, Any]) -> None:
    """Guarda el diccionario de normalización editado desde la interfaz."""
    path = CONFIG_DIR / "normalization.yaml"
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    invalidate()
