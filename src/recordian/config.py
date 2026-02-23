from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Pass2PolicyConfig:
    confidence_threshold: float = 0.88
    english_ratio_threshold: float = 0.15
    pass2_timeout_ms_local: int = 900
    pass2_timeout_ms_cloud: int = 1500


@dataclass(slots=True)
class AppConfig:
    policy: Pass2PolicyConfig = field(default_factory=Pass2PolicyConfig)
