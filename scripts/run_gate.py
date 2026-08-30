"""Phase 2 gate runner: N recorded text-chat interviews against the live RAG
MCP service and a live OpenAI-compatible LLM.

    INTERVIEW_LLM_BASE_URL=http://127.0.0.1:1234/v1 \
    INTERVIEW_LLM_MODEL=mlx-community/Qwen2.5-14B-Instruct-4bit \
    RAG_MCP_URL=http://127.0.0.1:8031/mcp \
    python scripts/run_gate.py --sessions 10 --questions 2

Every session summary is appended to
``gate_results/phase2-<date>.jsonl`` (one JSON object per line) and the
aggregate gate metrics are printed: wrap rate, rubric cache hit rate,
follow-up rate, per-hop latency means, and score coverage.
"""
import argparse
import asyncio
import json
import sys
import uuid
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interviewer.brain import LLMInterviewer
from interviewer.config import InterviewerConfig
from interviewer.demo import DEMO_ANSWERS
from interviewer.llm import LLMConfig, OpenAICompatibleLLM
from interviewer.rag_client import RagClient
from interviewer.state_machine import Session


async def run_sessions(args, config, *, out_dir: str | None = None) -> list[dict]:
    rag = RagClient(config.rag_mcp_url, token=config.rag_mcp_token)
    llm = OpenAICompatibleLLM(LLMConfig(
        base_url=config.llm_base_url, model=config.llm_model,
        token=config.llm_token,
    ))
    summaries = []
    out_path = None
    if out_dir:
        out_path = (Path(out_dir) / f"phase2-{date.today().isoformat()}.jsonl")
        out_path.parent.mkdir(exist_ok=True)
    for i in range(args.sessions):
        session = Session(
            session_id=f"gate-{date.today().isoformat()}-{i + 1:02d}",
            tenant_id="default", domain=args.domain,
        )
        interviewer = LLMInterviewer(rag, llm, session,
                                     max_questions=args.questions)
        summary = await interviewer.run(args.doc_id, answers=DEMO_ANSWERS)
        summaries.append(summary)
        if out_path:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(summary) + "\n")   # append: crash-resilient
        print(f"[{i + 1}/{args.sessions}] {summary['session_id']} "
              f"state={summary['state']} "
              f"scores={[e['scores'] for e in summary['scores']]} "
              f"cache={summary['stats']['cache_hit_rate']} "
              f"rag_mean={summary['stats']['rag_total_mean_ms']}ms "
              f"llm_ft_mean={summary['stats']['llm_first_token_mean_ms']}ms",
              flush=True)
    return summaries


def aggregate(summaries: list[dict]) -> dict:
    n = len(summaries)
    wrapped = sum(1 for s in summaries if s["state"] == "wrap")
    cache_rates = [s["stats"]["cache_hit_rate"] for s in summaries
                   if s["stats"]["cache_hit_rate"] is not None]
    followups = [e for s in summaries for e in s["scores"]]
    followup_rate = (sum(1 for e in followups if e["followup_asked"])
                     / len(followups)) if followups else None
    score_entries = [e for s in summaries for e in s["scores"]]
    score_coverage = (sum(1 for e in score_entries if len(e["scores"]) >= 2)
                      / len(score_entries)) if score_entries else None

    def mean(key):
        vals = [s["stats"][key] for s in summaries if s["stats"].get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    return {
        "sessions": n,
        "wrap_rate": wrapped / n,
        "rubric_cache_hit_rate_mean": round(sum(cache_rates) / len(cache_rates), 3)
                                      if cache_rates else None,
        "followup_rate": round(followup_rate, 3) if followup_rate is not None else None,
        "score_coverage_ge2_dims": round(score_coverage, 3) if score_coverage is not None else None,
        "llm_first_token_mean_ms": mean("llm_first_token_mean_ms"),
        "llm_total_mean_ms": mean("llm_total_mean_ms"),
        "rag_total_mean_ms": mean("rag_total_mean_ms"),
        "wall_mean_ms": mean("wall_ms"),
    }


async def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="run_gate", description="Phase 2 gate runner")
    p.add_argument("--sessions", type=int, default=10)
    p.add_argument("--questions", type=int, default=2)
    p.add_argument("--doc-id", default="bank-system-design")
    p.add_argument("--domain", default="system-design")
    p.add_argument("--out", default=None,
                   help="results directory (default: gate_results/)")
    args = p.parse_args(argv)
    config = InterviewerConfig.from_env()

    if not config.llm_model:
        print("INTERVIEW_LLM_MODEL is required (INTERVIEW_LLM_BASE_URL too "
              "for non-default endpoints)", file=sys.stderr)
        return 2

    summaries = await run_sessions(args, config, out_dir=args.out)
    agg = aggregate(summaries)

    out_dir = Path(args.out) if args.out else Path("gate_results")
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"phase2-{date.today().isoformat()}.jsonl"
    with out.open("a", encoding="utf-8") as fh:      # idempotent append; per-session
        for summary in summaries:                    # appends already happened via out_dir
            if not args.out:
                fh.write(json.dumps(summary) + "\n")

    print("\n=== Phase 2 gate aggregate ===")
    print(json.dumps(agg, indent=2))
    print(f"session records: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
