"""Command catalog loaded from rbvoice case files (rbvoice-cases.<lang>.json).

rbvoice stays the single source of truth: component names and parameter ids (keyword *values*)
are used verbatim. The catalog also produces the prompt section and the JSON-schema enum that
constrains the model to valid `component.parameter` pairs.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Which actions make sense per component (rbvoice behaviour). Unknown components allow all.
ALLOWED_ACTIONS: Dict[str, List[str]] = {
    "ems": ["GET"],
    "situation": ["GET"],
    "traffic": ["GET"],
    "airfields": ["GET"],
    "radio": ["GET", "SET"],
    "autopilot": ["GET", "SET", "DEL"],
    "timers": ["GET", "SET", "DEL"],
    "display": ["SET"],
}
# Max keyword examples per parameter in the prompt (keeps the cached prefix small)
EXAMPLES_PER_PARAM = 3


@dataclass
class Catalog:
    lang: str
    case_file: Dict[str, Any]
    source: str = ""
    params: Dict[str, List[str]] = field(default_factory=dict)  # "comp.param" -> spoken examples
    hash: str = ""

    @classmethod
    def from_case_file(cls, case_file: Dict[str, Any], source: str = "") -> "Catalog":
        lang = case_file.get("language", "")[:2]
        cat = cls(lang=lang, case_file=case_file, source=source)
        for comp, cfg in (case_file.get("components") or {}).items():
            keywords = cfg.get("keywords") or {}
            for phrase, param in keywords.items():
                key = f"{comp}.{param}"
                ex = cat.params.setdefault(key, [])
                if len(ex) < EXAMPLES_PER_PARAM:
                    ex.append(phrase)
            # Endpoints without any keyword are still valid parameters: the model can pick them by
            # meaning (e.g. the English case file has ems fuel1/fuel2 endpoints but no keywords).
            # Skipped when keyword values are not endpoint keys (display: values are page names).
            endpoints = set((cfg.get("endpoints") or {}).keys())
            if endpoints & set(keywords.values()):
                for ep in endpoints:
                    cat.params.setdefault(f"{comp}.{ep}", [])
        canon = json.dumps(case_file, sort_keys=True, ensure_ascii=False).encode("utf-8")
        cat.hash = hashlib.sha256(canon).hexdigest()[:16]
        return cat

    # --- accessors used by the tasks ----------------------------------------------------
    @property
    def aliases(self) -> Dict[str, str]:
        return self.case_file.get("aliases") or {}

    @property
    def wake_words(self) -> List[str]:
        return self.case_file.get("wake_words") or []

    @property
    def components(self) -> List[str]:
        return list((self.case_file.get("components") or {}).keys())

    def allowed_actions(self, component: str) -> List[str]:
        return ALLOWED_ACTIONS.get(component, ["GET", "SET", "DEL"])

    def param_keys(self) -> List[str]:
        return sorted(self.params)

    def prompt_section(self) -> str:
        """Static, deterministic text listing every parameter with spoken examples."""
        lines = []
        for comp in self.components:
            acts = "/".join(self.allowed_actions(comp))
            lines.append(f"[{comp}] actions: {acts}")
            for key in sorted(k for k in self.params if k.split(".", 1)[0] == comp):
                ex = ", ".join(f'"{e}"' for e in self.params[key]) or "(no examples)"
                lines.append(f"  {key}: {ex}")
        return "\n".join(lines)

    def stats(self) -> Dict[str, Any]:
        return {"lang": self.lang, "catalog_hash": self.hash,
                "components": len(self.components), "parameters": len(self.params)}


class CatalogStore:
    """Thread-safe set of catalogs, one per language."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_lang: Dict[str, Catalog] = {}

    def load_dir(self, directory: str) -> List[str]:
        loaded = []
        for path in sorted(glob.glob(os.path.join(directory, "rbvoice-cases.*.json"))):
            with open(path, encoding="utf-8") as f:
                cat = Catalog.from_case_file(json.load(f), source=path)
            if cat.lang:
                self.put(cat)
                loaded.append(cat.lang)
        return loaded

    def put(self, cat: Catalog) -> None:
        with self._lock:
            self._by_lang[cat.lang] = cat

    def get(self, lang: str) -> Optional[Catalog]:
        with self._lock:
            return self._by_lang.get(lang)

    def hashes(self) -> Dict[str, str]:
        with self._lock:
            return {k: v.hash for k, v in self._by_lang.items()}
