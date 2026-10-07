"""Editable native-settings snapshot; persistence uses the existing effect policy."""

import math
from copy import deepcopy
from pathlib import Path

from recordian.recommended_profile import (
    CREDENTIAL_KEYS,
    DICTATION_BUSY_STATUSES,
    confucius_endpoint_problem,
    http_cloud_endpoint_problem,
)
from recordian.setting_effects import SettingEffect, combined_setting_effect
from recordian.tray_utils import save_config_changes

INTEGERS = {"cooldown_ms", "sample_rate", "channels", "refine_max_tokens", "remote_paste_port", "wake_num_threads"}
FLOATS = {"duration", "asr_timeout_s", "remote_paste_timeout_s", "wake_auto_stop_silence_s"}
CSV_FIELDS = {"wake_prefix", "wake_name"}


class SettingsDraft:
    def __init__(self, current, defaults):
        self.defaults = deepcopy(defaults)
        self.saved = {
            key: deepcopy(current.get(key, value)) for key, value in defaults.items() if key not in CREDENTIAL_KEYS
        }
        self.values = deepcopy(self.saved)
        # A blank replacement preserves the saved secret; never put it in the
        # ordinary snapshot or restore-defaults mapping.
        self.refine_key = ""

    @property
    def dirty(self):
        return self.values != self.saved or bool(self.refine_key)

    def set_refine_key(self, value):
        self.refine_key = str(value).strip()

    def set(self, key, value):
        if key not in self.saved or key in CREDENTIAL_KEYS:
            raise ValueError("该字段不能在此编辑")
        self.values[key] = value

    def cancel(self):
        self.values = deepcopy(self.saved)
        self.refine_key = ""

    def restore(self):
        self.values = {key: deepcopy(self.defaults[key]) for key in self.saved}
        self.refine_key = ""

    def errors(self):
        errors = {}
        for key in INTEGERS | FLOATS:
            if key not in self.values:
                continue
            try:
                value = float(self.values[key])
                minimum = 0 if key == "cooldown_ms" else 1e-9
                if not math.isfinite(value) or value < minimum:
                    raise ValueError()
                if key in INTEGERS and not value.is_integer():
                    raise ValueError()
                if key == "remote_paste_port" and value > 65535:
                    raise ValueError()
            except (ValueError, TypeError):
                errors[key] = "请输入有效数值"
        provider = self.values.get("asr_provider")
        if provider == "confucius-asr":
            problem = confucius_endpoint_problem(str(self.values.get("asr_realtime_endpoint", "")))
            if problem:
                errors["asr_realtime_endpoint"] = problem
        elif provider == "http-cloud":
            for key in ("asr_realtime_endpoint", "asr_endpoint"):
                problem = http_cloud_endpoint_problem(str(self.values.get(key, "")))
                if problem:
                    errors[key] = problem
        if self.values.get("enable_text_refine"):
            cloud = self.values.get("refine_provider") == "cloud"
            model_key = "refine_api_model" if cloud else "refine_model"
            if model_key in self.values and not str(self.values[model_key]).strip():
                errors[model_key] = "请填写对应服务的模型名称或路径"
            if cloud and "refine_api_base" in self.values:
                endpoint = str(self.values["refine_api_base"]).strip()
                if not endpoint or http_cloud_endpoint_problem(endpoint):
                    errors["refine_api_base"] = "请填写有效的 HTTP 或 HTTPS API 地址"
        if "hotkey" in self.values and not str(self.values["hotkey"]).strip():
            errors["hotkey"] = "请设置听写按键"
        if self.values.get("enable_remote_paste") and not str(self.values.get("remote_paste_host", "")).strip():
            errors["remote_paste_host"] = "请填写远程主机"
        return errors

    def changes(self):
        result = {}
        for key, value in self.values.items():
            if value == self.saved[key]:
                continue
            if key in INTEGERS:
                value = int(value)
            elif key in FLOATS:
                value = float(value)
            elif key in CSV_FIELDS and isinstance(value, str):
                value = [part.strip() for part in value.split(",") if part.strip()]
            result[key] = value
        if self.refine_key:
            result["refine_api_key"] = self.refine_key
        return result

    def persist(self, path: Path, *, apply_now: bool, status="idle", restart_callback=None):
        errors = self.errors()
        if errors:
            raise ValueError(next(iter(errors.values())))
        changes = self.changes()
        effect = combined_setting_effect(list(changes))
        if apply_now and status in DICTATION_BUSY_STATUSES and effect is SettingEffect.RESTART_REQUIRED:
            raise ValueError("请先结束当前听写，再保存设置")
        restart_dispatched = False

        def dispatch_restart():
            nonlocal restart_dispatched
            try:
                restart_callback()
            except Exception:
                # The configuration is already safely saved. Report that a
                # manual restart is needed instead of claiming the write failed.
                return
            restart_dispatched = True

        effect, _, keys = save_config_changes(
            path,
            changes,
            apply_now=apply_now,
            restart_callback=dispatch_restart if restart_callback is not None else None,
        )
        self.saved = deepcopy(self.values)
        self.refine_key = ""
        return effect, restart_dispatched, keys
