"""Backward-compatibility shim for the split tray modules.

All functionality has been moved to focused submodules:
- tray_utils      – pure utility functions
- tray_menu       – AppIndicator / menu building
- tray_settings   – GTK settings window
- tray_context_editor – context editor
- tray_diagnostics    – runtime diagnostics
- tray_speaker_wizard – speaker enrollment wizard
- tray_app        – TrayApp class and main() entry point

Importing from this module still works but new code should import
from the specific submodule directly.
"""
from __future__ import annotations

# Re-export everything that used to live in tray_gui.py
from recordian.tray_app import (
    RecentRunObservation,
    TrayApp,
    UiState,
    build_parser,
    handle_exception,
    main,
)
from recordian.tray_context_editor import open_context_editor
from recordian.tray_diagnostics import (
    collect_runtime_diagnostics as collect_runtime_diagnostics,
)
from recordian.tray_diagnostics import derive_openai_models_endpoint
from recordian.tray_diagnostics import (
    format_diagnostic_report as format_diagnostic_report,
)
from recordian.tray_menu import (
    build_appindicator_menu,
    collect_recent_runtime_rows,
    get_logo_path,
    list_tray_refine_presets,
    refresh_appindicator_preset_submenu,
    status_summary_label,
    sync_appindicator_preset_submenu,
    update_tray_menu,
)
from recordian.tray_settings import (
    load_hotkey_default_config,
    open_settings_gtk,
)
from recordian.tray_speaker_wizard import open_speaker_enrollment_wizard
from recordian.tray_utils import (
    blend_hex,
    build_gtk_hotkey_spec,
    export_auto_lexicon_db,
    format_hotkey_spec,
    hex_with_alpha,
    import_auto_lexicon_db,
    next_event_poll_delay_ms,
    normalize_hotkey_token,
    overlay_hide_delay_seconds,
    parse_bool,
    save_config_changes,
    sqlite_backup,
    truncate,
)

# Backward-compatible private-name aliases (tests still reference these)
_blend_hex = blend_hex
_collect_recent_runtime_rows = collect_recent_runtime_rows
_derive_openai_models_endpoint = derive_openai_models_endpoint
_export_auto_lexicon_db = export_auto_lexicon_db
_extract_recent_run_observation = TrayApp._extract_recent_run_observation
_format_diagnostic_report = format_diagnostic_report
_format_recent_run_log_suffix = TrayApp._format_recent_run_log_suffix
_hex_with_alpha = hex_with_alpha
_import_auto_lexicon_db = import_auto_lexicon_db
_load_hotkey_default_config = load_hotkey_default_config
_next_event_poll_delay_ms = next_event_poll_delay_ms
_overlay_hide_delay_seconds = overlay_hide_delay_seconds
_parse_bool = parse_bool
_save_config_changes = save_config_changes
_sqlite_backup = sqlite_backup
_status_summary_label = status_summary_label
_truncate = truncate

__all__ = [
    # tray_app
    "TrayApp",
    "UiState",
    "RecentRunObservation",
    "build_parser",
    "main",
    "handle_exception",
    # tray_utils
    "overlay_hide_delay_seconds",
    "next_event_poll_delay_ms",
    "sqlite_backup",
    "export_auto_lexicon_db",
    "import_auto_lexicon_db",
    "truncate",
    "normalize_hotkey_token",
    "format_hotkey_spec",
    "build_gtk_hotkey_spec",
    "parse_bool",
    "hex_with_alpha",
    "blend_hex",
    "save_config_changes",
    # tray_menu
    "get_logo_path",
    "build_appindicator_menu",
    "update_tray_menu",
    "list_tray_refine_presets",
    "refresh_appindicator_preset_submenu",
    "sync_appindicator_preset_submenu",
    "status_summary_label",
    "collect_recent_runtime_rows",
    # tray_diagnostics
    "collect_runtime_diagnostics",
    "format_diagnostic_report",
    # tray_settings
    "open_settings_gtk",
    "load_hotkey_default_config",
    # tray_context_editor
    "open_context_editor",
    # tray_speaker_wizard
    "open_speaker_enrollment_wizard",
]
