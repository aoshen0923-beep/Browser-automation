"""Configuration: quizpilot.toml in the working directory, overridden by env vars."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "quizpilot.toml"


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: str = ""
    timeout: float = 60.0


@dataclass
class BrowserConfig:
    cdp_url: str = "http://127.0.0.1:9222"
    profile_dir: str = "chrome-profile"


@dataclass
class KBConfig:
    path: str = "kb/kb.sqlite"
    downloads: str = "downloads"


@dataclass
class SolverConfig:
    top_k: int = 8


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    kb: KBConfig = field(default_factory=KBConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)
    root: Path = field(default_factory=Path.cwd)

    def resolve(self, p: str) -> Path:
        path = Path(p).expanduser()
        return path if path.is_absolute() else self.root / path


def _apply(section: object, values: dict) -> None:
    for key, value in values.items():
        if hasattr(section, key):
            setattr(section, key, value)


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or Path.cwd() / CONFIG_NAME
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        cfg.root = path.parent
        for name in ("llm", "browser", "kb", "solver"):
            _apply(getattr(cfg, name), data.get(name, {}))
    env = os.environ
    cfg.llm.api_key = env.get("DEEPSEEK_API_KEY", cfg.llm.api_key)
    cfg.llm.base_url = env.get("QUIZPILOT_BASE_URL", cfg.llm.base_url)
    cfg.llm.model = env.get("QUIZPILOT_MODEL", cfg.llm.model)
    cfg.browser.cdp_url = env.get("QUIZPILOT_CDP_URL", cfg.browser.cdp_url)
    cfg.llm.model = cfg.llm.model.strip()
    if "deepseek.com" in cfg.llm.base_url:
        # DeepSeek model names are all lowercase; "Deepseek-Flash" is rejected.
        cfg.llm.model = cfg.llm.model.lower()
    return cfg
