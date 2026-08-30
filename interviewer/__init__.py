"""mock-interviewer — real-time AI technical mock interviewer (voice chat).

Consumer side of the standalone enterprise-rag-core service: this package
speaks MCP to the RAG service and never imports it. The voice pipeline
(livekit-agents) is an optional extra — the core of this package runs
without any audio infrastructure.
"""

__version__ = "0.1.0"
