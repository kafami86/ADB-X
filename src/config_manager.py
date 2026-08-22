"""
config_manager.py

Reads/writes the human-editable JSON configuration file: global settings
plus a list of named backup jobs. Adding/removing/editing jobs is just
list manipulation followed by a save -- easy to do by hand in a text
editor too, which is the point.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import List, Optional

from .models import JobConfig, GlobalSettings
from .logger import get_logger

log = get_logger()

DEFAULT_CONFIG = {
    "settings": asdict(GlobalSettings()),
    "jobs": [],
}


class ConfigError(RuntimeError):
    pass


class ConfigManager:
    def __init__(self, path: str = "config.json"):
        self.path = path
        self.settings = GlobalSettings()
        self.jobs: List[JobConfig] = []

    def load(self, create_if_missing: bool = True) -> "ConfigManager":
        if not os.path.exists(self.path):
            if create_if_missing:
                self.save()
                log.info("Created a new default config at %s", self.path)
            else:
                raise ConfigError(f"Config file not found: {self.path}")
            return self

        with open(self.path, "r", encoding="utf-8") as f:
            try:
                raw = json.load(f)
            except json.JSONDecodeError as e:
                raise ConfigError(f"config.json is not valid JSON: {e}") from e

        settings_dict = raw.get("settings", {})
        self.settings = GlobalSettings(**{**asdict(GlobalSettings()), **settings_dict})

        self.jobs = []
        for j in raw.get("jobs", []):
            self.jobs.append(
                JobConfig(
                    name=j["name"],
                    source=j["source"],
                    destination=j["destination"],
                    device_serial=j.get("device_serial"),
                    enabled=j.get("enabled", True),
                    verification_mode=j.get("verification_mode"),
                )
            )
        return self

    def save(self):
        data = {
            "settings": asdict(self.settings),
            "jobs": [asdict(j) for j in self.jobs],
        }
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self.path)

    # ---------------------------------------------------------- job CRUD
    def get_job(self, name: str) -> Optional[JobConfig]:
        for j in self.jobs:
            if j.name.lower() == name.lower():
                return j
        return None

    def add_job(self, job: JobConfig):
        if self.get_job(job.name):
            raise ConfigError(f"A job named '{job.name}' already exists.")
        self.jobs.append(job)
        self.save()

    def remove_job(self, name: str) -> bool:
        job = self.get_job(name)
        if not job:
            return False
        self.jobs.remove(job)
        self.save()
        return True

    def update_job(self, name: str, **fields) -> bool:
        job = self.get_job(name)
        if not job:
            return False
        for k, v in fields.items():
            if v is not None and hasattr(job, k):
                setattr(job, k, v)
        self.save()
        return True
