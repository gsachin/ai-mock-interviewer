"""LatencyBudgetTracker: recording, aggregation, the budget bar, and the
1.5 s gate verdict."""
from interviewer.voice.budget import BUDGET_MS, LatencyBudgetTracker


def _stages(stt=300.0, rag=100.0, llm=400.0, tts=150.0):
    return {"stt_final_ms": stt, "rag_ms": rag,
            "llm_first_token_ms": llm, "tts_first_audio_ms": tts}


def test_aggregate_and_within_budget():
    tracker = LatencyBudgetTracker()
    tracker.record(_stages())                                   # 1050 ms ✓
    tracker.record(_stages(llm=900.0))                          # 1550 ms ✗
    agg = tracker.aggregate()
    assert agg["turns"] == 2
    assert agg["within_budget_rate"] == 0.5
    assert agg["total_ms_mean"] == 1300.0
    assert agg["stt_final_ms"] == 300.0
    assert agg["llm_first_token_ms"] == 650.0


def test_network_stage_added_by_default():
    tracker = LatencyBudgetTracker()
    turn = tracker.record({"rag_ms": 100.0})
    assert turn.stage_ms["network_ms"] == 100.0
    assert turn.total_ms == 200.0


def test_bar_renders_and_verdict():
    tracker = LatencyBudgetTracker()
    tracker.record(_stages())
    bar = tracker.bar()
    assert "✓" in bar and f"budget {BUDGET_MS:.0f}ms" in bar
    assert "300→300.0" in bar


def test_empty_tracker():
    assert LatencyBudgetTracker().aggregate() == {}
    assert LatencyBudgetTracker().bar() == "no turns recorded"
