from recordian.setting_effects import (
    SettingEffect,
    combined_setting_effect,
    effect_label,
    effect_status_message,
    setting_effect_for_key,
)


def test_setting_effect_for_known_keys() -> None:
    assert setting_effect_for_key("auto_hard_enter") is SettingEffect.IMMEDIATE
    assert setting_effect_for_key("refine_preset") is SettingEffect.NEXT_SESSION
    assert setting_effect_for_key("enable_voice_wake") is SettingEffect.RESTART_REQUIRED


def test_combined_setting_effect_uses_strictest_semantics() -> None:
    assert combined_setting_effect(["auto_hard_enter"]) is SettingEffect.IMMEDIATE
    assert combined_setting_effect(["auto_hard_enter", "refine_preset"]) is SettingEffect.NEXT_SESSION
    assert combined_setting_effect(["refine_preset", "enable_text_refine"]) is SettingEffect.RESTART_REQUIRED


def test_effect_messages_are_user_facing() -> None:
    assert effect_label(SettingEffect.IMMEDIATE) == "立即生效"
    assert effect_label(SettingEffect.NEXT_SESSION) == "下次录音生效"
    assert effect_label(SettingEffect.RESTART_REQUIRED) == "重启后端生效"
    assert effect_status_message(SettingEffect.IMMEDIATE, restarted=False) == "已保存，设置立即生效"
    assert effect_status_message(SettingEffect.NEXT_SESSION, restarted=False) == "已保存，设置会在下次录音时生效"
    assert effect_status_message(SettingEffect.RESTART_REQUIRED, restarted=False) == "已保存到配置文件；需要重启后端后生效"
    assert effect_status_message(SettingEffect.RESTART_REQUIRED, restarted=True) == "已保存并重启后端，设置已应用"
