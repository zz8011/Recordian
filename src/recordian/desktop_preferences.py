"""Desktop preferences backed by the existing unit and Agent profile contract.

No Agent process, model, task history or credential file is opened here.
Mutations happen only through an explicit settings save. Unit changes never
use --now and never install or start a service.
"""

import json
import os
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path

from recordian.agent_entry import AgentInstance
from recordian.config import ConfigManager

UNIT = "recordian-desktop.service"
DEFAULTS = {
    "start_on_login": False,
    "agent_instance": "new",
    "agent_client": "hermes",
    "agent_id": "desktop",
    "agent_name": "桌面 Agent",
    "agent_executable": "",
    "agent_workspace": "",
    "agent_home": "",
    "agent_transport": "cli",
    "agent_api_url": "",
    "agent_timeout_s": "1800",
}


class Autostart:
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run
        self.enabled = None

    def read(self):
        try:
            result = self.runner(
                ["systemctl", "--user", "show", UNIT, "--property=LoadState,UnitFileState"],
                capture_output=True, text=True, timeout=3, check=True,
            )
            state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            enabled = state.get("UnitFileState")
            self.enabled = (enabled == "enabled") if state.get("LoadState") == "loaded" and enabled in {"enabled", "disabled"} else None
        except (OSError, subprocess.SubprocessError):
            self.enabled = None
        return self.enabled

    def set(self, enabled, expected):
        current = self.read()
        if current is None or current != expected:
            raise ValueError("开机自启状态已改变或不可用，请重新打开设置核对。")
        if current == enabled:
            return
        try:
            self.runner(["systemctl", "--user", "enable" if enabled else "disable", UNIT],
                        capture_output=True, text=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            # A timed-out command may already have changed enablement.
            actual = self.read()
            if actual is None:
                raise ValueError("自启修改结果未知，请核对桌面服务状态。") from exc
            if actual != expected:
                try:
                    self.runner(["systemctl", "--user", "enable" if expected else "disable", UNIT],
                                capture_output=True, text=True, timeout=5, check=True)
                    if self.read() != expected:
                        raise ValueError("rollback state unconfirmed")
                except (ValueError, OSError, subprocess.SubprocessError) as rollback:
                    raise ValueError("自启修改失败且未能回退，请核对桌面服务状态。") from rollback
            raise ValueError("未保存开机自启，请检查用户服务权限。") from exc
        if self.read() != enabled:
            raise ValueError("自启修改结果未确认，请重新打开设置核对。")


@dataclass
class Receipt:
    before: bytes | None
    after: bytes | None
    values: dict
    startup_before: bool | None
    startup_changed: bool
    agent_changed: bool


class DesktopPreferencesStore:
    """Keep unrelated instances and unknown fields; refuse concurrent edits."""
    def __init__(self, agents_path, autostart=None):
        self.path = Path(agents_path)
        self.autostart = autostart or Autostart()
        self.before = self._read_bytes()
        self.document = self._decode(self.before)
        self.values = deepcopy(DEFAULTS)
        instances = self.document.get("instances", [])
        selected = self.document.get("default_agent")
        profile = next((p for p in instances if p.get("id") == selected), instances[0] if instances else None)
        if profile:
            self.values.update(self.profile_values(profile))

    def _read_bytes(self):
        try:
            with self.path.open("rb") as file:
                raw = file.read(1024 * 1024 + 1)
        except FileNotFoundError:
            return None
        if len(raw) > 1024 * 1024:
            raise ValueError("Agent 配置过大，未加载。")
        return raw

    @staticmethod
    def _decode(raw):
        if raw is None:
            return {"instances": []}
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Agent 配置格式损坏，未覆盖原文件。") from exc
        if not isinstance(value, dict) or not isinstance(value.get("instances", []), list):
            raise ValueError("Agent 配置格式无效。")
        if any(not isinstance(p, dict) for p in value.get("instances", [])):
            raise ValueError("Agent 实例格式无效。")
        return value

    @staticmethod
    def profile_values(profile):
        return {
            "agent_instance": str(profile.get("id", "new")),
            "agent_client": str(profile.get("kind", "hermes")),
            "agent_id": str(profile.get("id", "desktop")),
            "agent_name": str(profile.get("name", "桌面 Agent")),
            "agent_executable": str(profile.get("executable", "")),
            "agent_workspace": str(profile.get("workspace", "")),
            "agent_home": str(profile.get("home", "")),
            "agent_transport": str(profile.get("transport", "cli")),
            "agent_api_url": str(profile.get("api_url", "")),
            "agent_timeout_s": str(profile.get("timeout_s", 1800)),
        }

    def profiles(self):
        return [(str(p.get("id", "")), str(p.get("name") or p.get("id", ""))) for p in self.document.get("instances", []) if p.get("kind", "hermes") in {"hermes", "claude"}]

    def select_profile(self, ident):
        if ident == "new":
            result = {k: v for k, v in DEFAULTS.items() if k.startswith("agent_")}
            existing = {p.get("id") for p in self.document.get("instances", [])}
            suffix = 1
            while result["agent_id"] in existing:
                suffix += 1
                result["agent_id"] = f"desktop-{suffix}"
            return result
        profile = next((p for p in self.document.get("instances", []) if p.get("id") == ident), None)
        if profile is None or profile.get("kind", "hermes") not in {"hermes", "claude"}:
            raise ValueError("此实例没有可用执行适配。")
        return self.profile_values(profile)

    def load(self):
        return deepcopy(self.values)

    def refresh_autostart(self):
        value = self.autostart.read()
        if value is not None:
            self.values["start_on_login"] = value
        return value

    def validate(self, values):
        if type(values.get("start_on_login")) is not bool:
            raise ValueError("开机自启必须是开关值。")
        if not any(values.get(k) != self.values.get(k) for k in DEFAULTS if k.startswith("agent_")):
            return None
        data = {
            "id": values["agent_id"], "name": values["agent_name"], "kind": values["agent_client"],
            "executable": values["agent_executable"], "workspace": values["agent_workspace"],
            "home": values["agent_home"], "transport": values["agent_transport"],
            "api_url": values["agent_api_url"], "timeout_s": values["agent_timeout_s"],
        }
        # This is exactly the runtime's parser, not a guessed CLI contract.
        try:
            timeout = int(values["agent_timeout_s"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Agent 超时必须是整数秒。") from exc
        if not 30 <= timeout <= 7200:
            raise ValueError("Agent 超时应在 30–7200 秒之间。")
        instance = AgentInstance.parse(data)
        return instance

    def save(self, values):
        instance = self.validate(values)
        startup_changed = values["start_on_login"] != self.values["start_on_login"]
        if startup_changed and self.autostart.enabled is None:
            raise ValueError("尚未确认开机自启状态，未保存。")
        if instance and self._read_bytes() != self.before:
            raise ValueError("Agent 配置已被其他窗口修改，请重新打开后保存。")
        receipt = Receipt(self.before, self.before, self.load(), self.autostart.enabled, False, instance is not None)
        try:
            if startup_changed:
                self.autostart.set(values["start_on_login"], self.values["start_on_login"])
                receipt.startup_changed = True
            if instance:
                document = deepcopy(self.document)
                entries = document.setdefault("instances", [])
                existing = next((p for p in entries if p.get("id") == instance.id), None)
                if existing is None:
                    entries.append(asdict(instance))
                else:
                    existing.update(asdict(instance))
                document["default_agent"] = instance.id
                ConfigManager.save(self.path, document)
                receipt.after = self._read_bytes()
                self.before = receipt.after
                self.document = document
            self.values = deepcopy(values)
            return receipt
        except Exception:
            if receipt.startup_changed:
                try:
                    self.autostart.set(receipt.startup_before, values["start_on_login"])
                except Exception as rollback:
                    raise ValueError("Agent 保存失败，自启未能回退；请核对桌面服务状态。") from rollback
            raise

    def restore(self, receipt):
        if receipt.agent_changed:
            if self._read_bytes() != receipt.after:
                raise ValueError("Agent 配置又被修改，无法安全回退。")
            if receipt.before is None:
                self.path.unlink(missing_ok=True)
            else:
                fd, name = tempfile.mkstemp(prefix=self.path.name + ".restore.", dir=self.path.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(fd, "wb") as target:
                        target.write(receipt.before)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
            self.document = self._decode(receipt.before)
            self.before = self._read_bytes()
        if receipt.startup_changed:
            self.autostart.set(receipt.startup_before, self.values["start_on_login"])
        self.values = deepcopy(receipt.values)
