from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Pass2PolicyConfig:
    confidence_threshold: float = 0.88
    english_ratio_threshold: float = 0.15
    pass2_timeout_ms_local: int = 900
    pass2_timeout_ms_cloud: int = 1500


@dataclass(slots=True)
class AppConfig:
    policy: Pass2PolicyConfig = field(default_factory=Pass2PolicyConfig)


class ConfigManager:
    """统一的配置文件管理器"""

    @staticmethod
    def load(path: Path | str) -> dict[str, Any]:
        p = Path(path).expanduser()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def save(path: Path | str, config: dict[str, Any]) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
