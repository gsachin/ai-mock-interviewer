"""TEST-RERANK-01: reranker discrimination — real tokenization + real ONNX INT8
run against the pinned tokenizers/onnxruntime versions. Converted from the seed
verification harness (seed: test_tokenizer.py)."""
import pytest
from tokenizers import Tokenizer

from enterprise_rag.model import Chunk
from enterprise_rag.reranker import ONNXVoiceReranker


@pytest.fixture(scope="module")
def tokenizer():
    tok = Tokenizer.from_pretrained("cross-encoder/ms-marco-MiniLM-L-6-v2")
    tok.enable_truncation(max_length=128)
    tok.enable_padding(pad_id=0, pad_token="[PAD]")
    return tok


def test_tokenizer_pair_encoding_distinctness(tokenizer):
    pairs = [
        (
            "evaluate leadership under pressure",
            "Behavioral rubric: evaluates situational leadership under pressure.",
        ),
        (
            "evaluate leadership under pressure",
            "Coding standard: class isolation requirements in Java.",
        ),
    ]
    enc = tokenizer.encode_batch(pairs)
    ids = [e.ids for e in enc]
    assert ids[0] != ids[1]
    assert all(len(i) <= 128 for i in ids), f"{[len(i) for i in ids]}"
    assert all(e.type_ids is not None for e in enc)
    assert 0 in enc[0].type_ids and 1 in enc[0].type_ids, f"{enc[0].type_ids}"
    assert all(e.attention_mask is not None for e in enc)


@pytest.fixture(scope="module")
def reranker(reranker_model_path):
    if reranker_model_path is None:
        pytest.skip("reranker model not downloaded (run: enterprise-rag-core download-model)")
    return ONNXVoiceReranker(str(reranker_model_path))


def test_reranker_discrimination(reranker):
    query = "evaluate situational leadership under pressure"
    chunks = [
        Chunk("A", "rub1", "acme", "Behavioral rubric: evaluates situational leadership under pressure.", 0.9),
        Chunk("B", "rub2", "acme", "Behavioral rubric: team collaboration and communication.", 0.8),
        Chunk("C", "rub3", "acme", "System design rubric: microservice isolation techniques.", 0.7),
        Chunk("D", "rub4", "acme", "Coding standard: class isolation requirements in Java.", 0.6),
        Chunk("E", "rub5", "acme", "Compensation policy: PTO accrual tiers for staff.", 0.5),
    ]
    ranked = reranker.rerank(query, chunks)
    assert len(ranked) == 5
    scores = [c.score for c in ranked]
    assert len(set(scores)) == 5, f"{scores}"
    pos = {c.chunk_id: i for i, c in enumerate(ranked)}
    assert pos["A"] < pos["E"], f"order={[c.chunk_id for c in ranked]}"


def test_reranker_empty_input(reranker):
    assert reranker.rerank("q", []) == []
