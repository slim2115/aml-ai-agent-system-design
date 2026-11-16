# Retrieval v1: immutable demonstration snapshots

`src/runtime/retrieval.py` replaces the live use of the legacy Chroma collection.
The small local prototype corpus is indexed in memory. A new `RuleIndex` becomes
available only after complete validation, chunking and embedding. No mutable
collection is reused, so deleting a rule or changing text under the same rule ID
cannot leave an old vector in the new index. Existing readers keep their complete
old immutable snapshot until their owner replaces it. Failed construction does
not publish a partial snapshot. This design trades startup work and memory for a
smaller, reviewable prototype implementation; it is not a persistent shared index
or a distributed atomic deployment mechanism.

## Identity and exact evidence

The index fingerprint binds the complete normalized corpus, the SHA-256 digest
of the verified embedding manifest, the chunk recipe, actual tokenizer class and
actual model input limit, plus the complete resulting chunk inventory digest.
File-backed indexes also bind the original source bytes
as `source_sha256`; callers compare this with the coherent policy/source snapshot.
The default embedding factory verifies all local artifact files before loading,
uses CPU and local files only, and has no model download fallback. Injected
factories and `from_records` are trusted testing/integration seams, not independent
artifact verification paths.

Each citation binds rule ID, version, exact source reference, chunk ID and exact
UTF-8 chunk-text digest. The chunk ID includes version, source reference, offsets
and text digest. Offsets refer to the source rule's `text` field, and the source
text SHA-256 is retained separately. Titles and other corpus fields participate
in corpus identity even though only exact rule text is embedded. The citation
catalog is an immutable tuple, and returned provenance dictionaries are copies.

## Chunking without silent truncation

Chunk boundaries use offset mappings from the actual loaded fast tokenizer.
The actual sentence-transformer `max_seq_length`, rather than only the tokenizer's
advertised maximum, sets the input limit. The recipe reserves special tokens and
uses up to 16 overlapping tokens (a quarter of a smaller available budget).
Every exact text substring is tokenized again, with special tokens and truncation
disabled, and must fit both the model limit and the v1 4,000-character text limit.
Otherwise the boundary shrinks; an unrepresentable token fails the build instead
of being dropped. All source characters, including boundary whitespace, remain
covered. Query texts are likewise checked against the model input limit.

## Editions and ranking

The optional `effective_until` is exclusive. Edition dates use **UTC midnight as
an explicit synthetic demonstration convention**; no legal timezone or production
policy has been approved. Overlapping editions of the same rule ID are rejected
as ambiguous, and duplicate ID/version pairs, invalid dates, empty or malformed
corpora prevent readiness. A newer edition does not implicitly close an older
one. The aware `as_of` timestamp is converted to UTC before active-edition
filtering; this happens before ranking and top-k selection.

`search(query, as_of=..., k=...)` accepts only known alert/category enum strings:
`structuring`, `threshold`, `threshold_breach`, `kyc`, `kyc_mismatch`, `high_risk`,
`high_risk_country`, `reporting`, `velocity`. They map to fixed query phrases;
raw client names, transaction purposes and free-form instructions cannot become
the retrieval query. All finite nonzero vectors are normalized, cosine distance
is ranked, and chunk identity breaks ties deterministically. There is **no
invented relevance threshold or claim that a ranked rule is applicable**.
`active_chunk_count(as_of)` lets the caller retrieve all active candidates before
an independently justified policy selection. `RetrievedChunk.as_applicable()` is
only a v1 shape conversion, not a policy decision.

The corpus remains synthetic and legally unapproved. Rule text is untrusted data;
retrieval alone neither proves legal correctness nor neutralizes instructions
inside a corpus. The runtime must independently enforce corpus identity, policy,
generation constraints and exact citation binding. No rule text is logged by this
module. Failures expose only a stable code, with no raw path, source or provider
exception detail.

## Verification boundary

Hermetic tests cover long overlapping chunks, complete source coverage, edition
boundaries/timezones, version/hash binding, corpus changes/removal, failed builds,
input allowlisting and invalid vectors. Their deterministic fake vectors test
invariants, **not retrieval relevance**. Real tokenizer/model qualification is a
separate local probe. Production recall/precision and legal applicability still
require independent labeled examples and expert-approved policy/corpus versions.
