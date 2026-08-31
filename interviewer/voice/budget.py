"""Voice round-trip latency budget (utterance-end -> first interviewer audio).

The Phase 3 gate: median under 1500 ms. Stage budgets follow the feasibility
study's model — STT final 300, RAG 150, LLM first token 500, TTS first audio
200, network 100 ms. Streaming overlaps these (TTS starts on sentence one),
so the sum is a conservative upper bound.
"""
from dataclasses import dataclass, field

BUDGET_MS = 1500.0

STAGE_BUDGETS = {
    "stt_final_ms": 300.0,
    "rag_ms": 150.0,
    "llm_first_token_ms": 500.0,
    "tts_first_audio_ms": 200.0,
    "network_ms": 100.0,
}


@dataclass
class TurnLatency:
    stage_ms: dict[str, float] = field(default_factory=dict)
    total_ms: float = 0.0

    @property
    def within_budget(self) -> bool:
        return self.total_ms <= BUDGET_MS


class LatencyBudgetTracker:
    """Collects per-turn stage timings; aggregates for the gate."""

    def __init__(self) -> None:
        self.turns: list[TurnLatency] = []

    def record(self, stage_ms: dict[str, float], network_ms: float = 100.0) -> TurnLatency:
        stages = dict(stage_ms)
        stages.setdefault("network_ms", network_ms)
        turn = TurnLatency(stage_ms=stages, total_ms=round(sum(stages.values()), 1))
        self.turns.append(turn)
        return turn

    def aggregate(self) -> dict[str, float]:
        if not self.turns:
            return {}
        n = len(self.turns)

        def mean(key: str) -> float:
            vals = [t.stage_ms.get(key, 0.0) for t in self.turns]
            return round(sum(vals) / n, 1)

        return {
            "turns": n,
            "within_budget_rate": round(
                sum(1 for t in self.turns if t.within_budget) / n, 3),
            "total_ms_mean": round(sum(t.total_ms for t in self.turns) / n, 1),
            **{key: mean(key) for key in STAGE_BUDGETS},
        }

    def bar(self) -> str:
        """One-line budget bar for logs/telemetry."""
        if not self.turns:
            return "no turns recorded"
        agg = self.aggregate()
        parts = " | ".join(
            f"{STAGE_BUDGETS[k]:.0f}→{agg[k]}" for k in STAGE_BUDGETS)
        mark = "✓" if agg["total_ms_mean"] <= BUDGET_MS else "✗"
        return f"[{parts}] total {agg['total_ms_mean']}ms {mark} (budget {BUDGET_MS:.0f}ms)"
