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