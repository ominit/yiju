from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Analysis:
    tokens: tuple[str, ...]
    unknown: tuple[str, ...]
    coverage: float
    score: float
    eligible: bool
    reason: str


def analyze(
    tokens: tuple[str, ...],
    known: frozenset[str],
    target: str,
    max_unknown: int = 2,
    *,
    include_zero: bool = False,
) -> Analysis:
    unknown_tokens = tuple(token for token in tokens if token not in known)
    unknown = tuple(dict.fromkeys(unknown_tokens))
    coverage = 1 - len(unknown_tokens) / len(tokens) if tokens else 0.0
    reason = "Ready"
    if not tokens:
        reason = "No words found"
    elif target not in tokens:
        reason = "Target word is not a separate token"
    elif len(unknown) > max_unknown:
        reason = f"{len(unknown)} unfamiliar words; limit is {max_unknown}"
    elif not unknown and not include_zero:
        reason = "All words are familiar"
    preference = {1: 300, 2: 200, 0: 100}.get(len(unknown), 0)
    score = preference + 20 * coverage - min(len(tokens), 60) - len(unknown_tokens)
    return Analysis(tokens, unknown, coverage, score, reason == "Ready", reason)
