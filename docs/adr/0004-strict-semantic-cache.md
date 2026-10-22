# 4. Serve semantic cache hits only when the meaning words match

**Decision.** Embeddings find candidates; a hit is served only if both prompts have the same
normalised content words (numbers, names and negations included).

**Why.** A wrong cached answer is worse than a cache miss. The strict check served 90.8% of repeats
and none of the near misses in the evaluation; true paraphrases with different words are missed by
design. Loosening it requires measuring false hits first.
