"""Phase 2 demo: a full scripted interview against the live RAG MCP service
and a live OpenAI-compatible LLM.

    INTERVIEW_LLM_BASE_URL=http://127.0.0.1:1234/v1 \
    INTERVIEW_LLM_MODEL=mlx-community/Qwen2.5-14B-Instruct-4bit \
    RAG_MCP_URL=http://127.0.0.1:8031/mcp \
    python -m interviewer.demo --questions 2

``--stub-llm`` swaps in canned LLM turns for CI-style runs without a model.
"""
import argparse
import asyncio
import json
import sys
import uuid

from interviewer.brain import LLMInterviewer
from interviewer.config import InterviewerConfig
from interviewer.llm import LLMConfig, OpenAICompatibleLLM
from interviewer.rag_client import RagClient
from interviewer.session_store import InMemorySessionStore, RedisSessionStore
from interviewer.state_machine import Session
from interviewer.voice.stubs import StubLLM

DEMO_ANSWERS = {
    # system-design bank
    "s1": "Token bucket with per-user keys in Redis, atomic INCR with expiry, "
          "and 429 responses with retry-after headers.",
    "s1:followup": "Sliding windows smooth bursts better than fixed windows, "
                   "at the cost of more state.",
    "s2": "Virtual nodes on a hash ring, so only K/N keys move when a node "
          "joins or leaves.",
    "s2:followup": "It applies to distributed caches and sharded databases "
                   "by keeping reshuffling minimal.",
    "s3": "Base62-encoded ids from a distributed counter, with a cache for "
          "hot short codes and analytics off the redirect path.",
    "s4": "Idempotency keys with request fingerprints and a TTL prevent "
          "retry storms and give payment APIs exactly-once semantics.",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="interviewer.demo",
                                description="Phase 2 scripted interview demo")
    p.add_argument("--doc-id", default="bank-system-design")
    p.add_argument("--domain", default="system-design")
    p.add_argument("--questions", type=int, default=2)
    p.add_argument("--session-id", default=None)
    p.add_argument("--stub-llm", action="store_true",
                   help="use canned LLM turns instead of a live model")
    p.add_argument("--json", action="store_true",
                   help="print only the final summary JSON")
    return p


async def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = InterviewerConfig.from_env()

    rag = RagClient(config.rag_mcp_url, token=config.rag_mcp_token)
    if args.stub_llm:
        llm = StubLLM([
            "Welcome to your system design interview.",
            "Design a rate limiter for a public API.",
            "Explain consistent hashing and why it minimizes reshuffling.",
            "Why do distributed systems need idempotency keys?",
            "That concludes the interview. Your average score was 4.0 out of 5.",
        ])
    else:
        llm = OpenAICompatibleLLM(LLMConfig(
            base_url=config.llm_base_url, model=config.llm_model,
            token=config.llm_token,
        ))

    session = Session(
        session_id=args.session_id or uuid.uuid4().hex[:12],
        tenant_id="default",
        domain=args.domain,
    )
    interviewer = LLMInterviewer(rag, llm, session, max_questions=args.questions)
    summary = await interviewer.run(args.doc_id, answers=DEMO_ANSWERS)

    store = (RedisSessionStore(config.redis_url) if config.session_store == "redis"
             else InMemorySessionStore())
    await store.save(summary["session_id"], summary)

    if not args.json:
        print("=" * 72)
        print(f"SESSION {summary['session_id']}  domain={summary['domain']}  "
              f"state={summary['state']}")
        print("=" * 72)
        for turn in summary["turns"]:
            print(f"\n[{turn['role'].upper()}] {turn['text']}")
        print("\n--- scores ---")
        for entry in summary["scores"]:
            print(f"  {entry['question_id']} ({entry['section_title']}): "
                  f"{entry['scores']} followup={entry['followup_asked']}")
        print("\n--- stats ---")
        print(json.dumps({k: v for k, v in summary["stats"].items()
                          if k != "hops"}, indent=2))
    else:
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
