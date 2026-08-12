## A/B Model Comparison (Iteration 8)

### Baseline: llama3.1:8B (4.9 GB)
- Schema Match: 0% (arrays in evidence_ref, invalid field values)
- Traceability: 0% (hallucinated rule_ids R-115-003, R-115-005)
- Evidence Grounding: 50% (empty evidence_ref, Russian field names)
- Refusal Correctness: 100%

### Improved: qwen2.5:14b (9.0 GB)
- Schema Match: 100% (after prompt v0.4 + normalizer)
- Traceability: 100% (only real rule_ids from knowledge base)
- Evidence Grounding: 100% (valid tx_id, valid field enum)
- Refusal Correctness: 100%

### Conclusion
qwen2.5:14b provides significantly better structured output adherence while running on 16GB RAM. Recommended as default model for PoC.

## Iteration 9: End-to-End Agent Orchestration (LangGraph)

**Result:** happy-path case INC-000123 passes all guardrails.

| Guard | Status |
|---|---|
| prompt_injection | PASS |
| schema_match | PASS |
| traceability | PASS |
| evidence_grounding | PASS |

Model: qwen2.5:14b (Ollama, on-premise). Retrieval: paraphrase-multilingual-MiniLM-L12-v2.
Defects discovered and fixed during stabilization: 6 (source_ref brackets, rule_id hallucinations,
evidence arrays, format leakage, missing confidence, read timeout).

## Final Evaluation Run (2026-08-12)

**Environment:** Windows 11, qwen2.5:14b (Ollama, CPU), paraphrase-multilingual-MiniLM-L12-v2.
**Test duration:** 464.86s (3 LLM cases including retry cycles).

| Metric | Target | Actual | Status |
|---|---|---|---|
| M-01 Schema Match | ≥0.9 | 1.0 | ✅ PASSED |
| M-04 Traceability | ≥0.95 | 1.0 | ✅ PASSED |
| M-05 Refusal Correctness | ≥0.95 | 1.0 | ✅ PASSED |
| M-06 Evidence Grounding | 1.0 | 1.0 | ✅ PASSED |
| M-07 RAG Recall | ≥0.9 | 1.0 | ✅ PASSED |
| M-09 Max Latency | ≤600s | 135.41s | ✅ PASSED |

**Quality gates:** ALL PASSED

### Stabilization journey
1. Baseline (llama3.1:8B): hallucinated rule_ids, bracketed source_ref, fictional dates.
2. Model upgrade (qwen2.5:14b): eliminated rule_id hallucinations, better schema adherence.
3. Normalizer: 6 defect classes handled (brackets, arrays, rule_id auto-correction,
   format leakage, missing confidence, read timeout via capped expansion).
4. Retry logic (3 attempts): compensates residual LLM non-determinism.

**Conclusion:** local on-premise LLM + strict schema + multi-level guardrails + retry
delivers reliable AML draft generation with 100% code-based quality metrics.