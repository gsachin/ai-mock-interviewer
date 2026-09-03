# Verbatim transcript of TRD_ENTERPRISE_RAG_MCP_CORE.md §2.3 — used for verification only.
import numpy as np
import onnxruntime as ort
from dataclasses import replace
from tokenizers import Tokenizer

from enterprise_rag.model import Chunk


class ONNXVoiceReranker:
    def __init__(self, model_path: str,
                 tokenizer_id: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
                 max_length: int = 128):
        # Low-latency ONNX session config (unchanged from v2026.4 — verified correct)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        # Load INT8-quantized cross-encoder
        self._session = ort.InferenceSession(model_path, opts, providers=["CPUExecutionProvider"])
        self._output_name = self._session.get_outputs()[0].name   # export-dependent, never hardcode

        # Pair encoder: (query, chunk) -> input_ids / attention_mask / token_type_ids
        # Requires tokenizer.json in the model repo (MiniLM ships it).
        # Fallback if absent: transformers.AutoTokenizer.from_pretrained(...).
        self._tokenizer = Tokenizer.from_pretrained(tokenizer_id)
        self._tokenizer.enable_truncation(max_length=max_length)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

    def _encode_pairs(self, query: str, chunks: list[Chunk]):
        encodings = self._tokenizer.encode_batch([(query, c.content) for c in chunks])
        return {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            # token_type_ids: 0 = query segment, 1 = passage segment. MiniLM ONNX
            # exports require all three inputs.
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }

    def rerank(self, query: str, chunks: list[Chunk]) -> list[Chunk]:
        if not chunks:
            return []
        inputs = self._encode_pairs(query, chunks)      # distinct pairs -> distinct tensors
        # target: <= 11 ms p95 on CPU for <= 8 candidates
        logits = self._session.run([self._output_name], inputs)[0]   # (N, 1)
        scored = [
            replace(c, score=float(s))
            for c, s in zip(chunks, logits[:, 0])
        ]
        # Raw logits rank identically to sigmoid(logits) (monotonic) — skip the
        # sigmoid; report scores as relevance logits in provenance.
        return sorted(scored, key=lambda c: -c.score)


class NoOpReranker:
    """Reranking disabled — the fused RRF order is kept as-is."""

    def rerank(self, query: str, chunks: list[Chunk]) -> list[Chunk]:
        return list(chunks)
