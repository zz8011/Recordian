"""Preset 管理器：加载和管理文本精炼 prompt 预设"""

from __future__ import annotations

from pathlib import Path


class PresetManager:
    """管理 prompt 预设文件"""

    def __init__(self, presets_dir: str | Path = "presets") -> None:
        self.presets_dir = Path(presets_dir)
        if not self.presets_dir.is_absolute():
            # 相对于项目根目录
            self.presets_dir = Path(__file__).parent.parent.parent / self.presets_dir

    def list_presets(self) -> list[str]:
        """列出所有可用的预设名称"""
        if not self.presets_dir.exists():
            return []
        return sorted([
            p.stem for p in self.presets_dir.glob("*.md")
        ])

    def load_preset(self, name: str) -> str:
        """加载指定预设的 prompt 内容

        Args:
            name: 预设名称（不含 .md 后缀）

        Returns:
            预设的 prompt 内容

        Raises:
            FileNotFoundError: 预设文件不存在
        """
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"非法预设名称: {name!r}")
        preset_path = self.presets_dir / f"{name}.md"
        if not preset_path.exists():
            available = ", ".join(self.list_presets())
            raise FileNotFoundError(
                f"预设 '{name}' 不存在。可用预设: {available}"
            )

        content = preset_path.read_text(encoding="utf-8")

        # 移除第一行标题（如果存在）
        lines = content.strip().split("\n")
        if lines and lines[0].startswith("#"):
            lines = lines[1:]

        return "\n".join(lines).strip()

    def get_preset_path(self, name: str) -> Path:
        """获取预设文件的完整路径"""
        return self.presets_dir / f"{name}.md"

    def preset_exists(self, name: str) -> bool:
        """检查预设是否存在"""
        return self.get_preset_path(name).exists()
