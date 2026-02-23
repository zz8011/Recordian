from recordian.benchmark import (
    GateThresholds,
    char_error_rate,
    edit_distance,
    evaluate_summary_gates,
    normalize_text,
    percentile,
)


def test_normalize_text_mixed_language() -> None:
    text = "开饭 time: 9:00 到 5pm。"
    assert normalize_text(text) == "开饭time900到5pm"


def test_edit_distance_basic() -> None:
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance("", "abc") == 3
    assert edit_distance("abc", "abc") == 0


def test_char_error_rate_and_percentile() -> None:
    cer, errors, ref_chars = char_error_rate("开放时间", "开饭时间")
    assert ref_chars == 4
    assert errors == 1
    assert cer == 0.25

    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 50) == 25.0
    assert percentile(values, 0) == 10.0
    assert percentile(values, 100) == 40.0


def test_evaluate_summary_gates_pass_and_fail() -> None:
    summary = {
        "global_cer": 0.06,
        "latency_ms_p90": 900.0,
        "failure_rate": 0.01,
        "rtf_avg": 0.35,
    }
    thresholds_pass = GateThresholds(
        max_global_cer=0.08,
        max_latency_p90_ms=1200.0,
        max_failure_rate=0.02,
        max_rtf_avg=0.5,
    )
    assert evaluate_summary_gates(summary, thresholds_pass) == []

    thresholds_fail = GateThresholds(
        max_global_cer=0.05,
        max_latency_p90_ms=800.0,
        max_failure_rate=0.005,
        max_rtf_avg=0.3,
    )
    failures = evaluate_summary_gates(summary, thresholds_fail)
    assert len(failures) == 4
