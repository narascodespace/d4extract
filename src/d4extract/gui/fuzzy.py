"""Subsequence fuzzy matcher with fzf-style scoring.

The model list browses ~67K SNO paths. The previous filter ran a
case-insensitive substring check inside ``QSortFilterProxyModel.filterAcceptsRow``,
which crosses the C++/Python bridge once per row per keystroke. This
module replaces it with a pure-Python subsequence scorer that runs in
bulk on a worker thread, with a small DP that picks the highest-scoring
alignment rather than the first subsequence found.

Scoring (all bonuses summed for each matched character, then optionally
multiplied):

* Word boundary  (+8) — match immediately after ``_``, ``/``, ``.``,
  ``-`` or at the start of the target.
* camelCase     (+7) — match on an uppercase letter preceded by a
  lowercase letter.
* First char    (+8) — query's first char matches target's first char.
* Consecutive   (+4) — char matched contiguously with the previous
  matched char.
* Filename       (×2) — matches in the segment after the last ``/``
  count double; SNO paths put the meaningful name there.

The function returns ``(score, matched_indices)`` so the delegate can
paint the matched characters in the accent color, or ``None`` if the
query is not a subsequence of the target.
"""

from __future__ import annotations

# Sentinel for unreachable DP cells. Stays well below any real score so
# arithmetic on it is still safely outside the legitimate range.
_NEG = -10**9


def _bonus_at(target: str, j: int) -> int:
    """Per-character positional bonus, ignoring consecutive / first-char."""
    if j == 0:
        # Start of string is a hard word boundary.
        return 8
    prev = target[j - 1]
    cur = target[j]
    if prev in ("_", "/", ".", " ", "-"):
        return 8
    if prev.islower() and cur.isupper():
        return 7
    return 0


def fuzzy_score(query: str, target: str) -> tuple[int, list[int]] | None:
    """Return ``(score, matched_indices)`` or ``None`` if no match.

    Matching is case-insensitive. ``matched_indices`` are positions into
    the original (un-lowercased) ``target`` string, in ascending order
    and one entry per query character.
    """
    if not query:
        return 0, []

    q = query.lower()
    t_lower = target.lower()
    m = len(q)
    n = len(t_lower)
    if m > n:
        return None

    # Cheap pre-screen: a left-to-right subsequence walk decides
    # membership without allocating a DP table. The vast majority of
    # 67K paths fail this on a typical query, and we skip the O(m·n)
    # cost entirely for them.
    qi = 0
    for ch in t_lower:
        if ch == q[qi]:
            qi += 1
            if qi == m:
                break
    if qi < m:
        return None

    last_slash = target.rfind("/")
    first_char_match = q[0] == t_lower[0]

    # score[i][j] = best score for query[0..=i] consuming target[0..=j]
    # WITH q[i] matched at t[j]. Cells that aren't reachable stay _NEG.
    # parent[i][j] holds the j' from row i-1 that produced this score —
    # backtracking from the best cell of the last row gives the matched
    # indices.
    score = [[_NEG] * n for _ in range(m)]
    parent = [[-1] * n for _ in range(m)]

    # Row 0: matching just q[0] against any target index.
    for j in range(n):
        if t_lower[j] == q[0]:
            b = _bonus_at(target, j)
            if j == 0 and first_char_match:
                # First-char bonus stacks with the start-of-string boundary.
                b += 8
            mult = 2 if j > last_slash else 1
            score[0][j] = b * mult

    # Subsequent rows. For each (i, j) where q[i] matches t[j] we pick
    # the best of:
    #   - any prior path ending at j' < j (running max from row i-1)
    #   - the consecutive transition from j-1 (which earns the +4 bonus)
    # The running max collapses the inner loop from O(j) to O(1).
    for i in range(1, m):
        running_max = _NEG
        running_max_j = -1
        for j in range(i, n):
            # Fold score[i-1][j-1] into the running max BEFORE we use it
            # for this column. After this point running_max = best of
            # score[i-1][0..j-1], which is exactly what the
            # non-consecutive transition needs.
            prior = score[i - 1][j - 1]
            if prior > running_max:
                running_max = prior
                running_max_j = j - 1

            if t_lower[j] != q[i]:
                continue

            b = _bonus_at(target, j)
            mult = 2 if j > last_slash else 1

            best = _NEG
            best_parent = -1

            if running_max > _NEG:
                cand = running_max + b * mult
                if cand > best:
                    best = cand
                    best_parent = running_max_j

            # Consecutive option uses the same j-1 ancestor as the
            # running-max path but adds +4 for the contiguous chain. It
            # always dominates the non-consecutive option from j-1, so
            # we end up picking it whenever j-1 is the best ancestor.
            if prior > _NEG:
                consec = prior + (b + 4) * mult
                if consec > best:
                    best = consec
                    best_parent = j - 1

            if best > _NEG:
                score[i][j] = best
                parent[i][j] = best_parent

    # Pick the best terminal cell across the last row.
    last = score[m - 1]
    best_score = _NEG
    best_j = -1
    for j in range(m - 1, n):
        if last[j] > best_score:
            best_score = last[j]
            best_j = j

    if best_score == _NEG:
        # Subsequence walk said this should match — only reachable here
        # if the DP rows fell out of sync with the prescreen, which
        # shouldn't happen. Treat as no match.
        return None

    # Backtrack the parent chain for the matched indices.
    indices: list[int] = [0] * m
    j = best_j
    for i in range(m - 1, -1, -1):
        indices[i] = j
        j = parent[i][j]

    return best_score, indices


# Separators that mark a "word" boundary for the multi-token substring
# scorer. Mirrors the segmenters that show up in real SNO paths plus
# the literal characters the spec calls out (``_``, ``/``, ``.``); kept
# distinct from ``_bonus_at`` because the subsequence and substring
# scorers serve different intents (abbreviation vs keyword).
_SUBSTR_SEPARATORS = ("_", "/", ".")

# Tokens this length or longer get substring matching; shorter tokens
# stay in subsequence mode. The threshold sits at 3 because real-world
# usage tends to bottom out at 2-letter abbreviations like ``ms`` or
# ``sp``; once the user has typed a third character they almost always
# mean it literally (``bar`` → barbarians, ``barf``/``barm`` → female
# vs male variants). Anything that lets ``msl`` subsequence-match
# ``monster_spider_large`` also lets ``bar`` scatter across unrelated
# paths, which is the regression this guards against.
_SUBSTRING_TOKEN_LEN = 3


def _substring_token_score(
    token: str, target: str,
) -> tuple[int, list[int]] | None:
    """Best-occurrence case-insensitive substring score for one token.

    Returns ``(score, indices)`` for the highest-scoring occurrence of
    ``token`` in ``target``, or ``None`` if no occurrence exists. Score
    composition (per spec):

    * Base ``len(token) * 4`` — longer literal hits beat shorter ones.
    * +8 if the match starts at a word boundary (immediately after
      ``_``, ``/``, ``.`` or at index 0).
    * +16 if the match is bounded by separators on both sides — i.e.
      a whole-word hit. This *replaces* the +8 (an exact word is also
      a word boundary; it's a stronger condition, not an additive one).
    * ×2 if the match falls in the filename segment (after the last
      ``/``).

    When the token appears more than once we keep the highest-scoring
    position so e.g. ``"_spider_"`` beats a later inline ``spider`` on
    the same target.
    """
    if not token:
        return 0, []

    t_lower = target.lower()
    tok_lower = token.lower()
    n = len(target)
    L = len(token)
    if L > n:
        return None

    last_slash = target.rfind("/")
    base = L * 4

    best_score = -1
    best_pos = -1

    pos = 0
    while True:
        i = t_lower.find(tok_lower, pos)
        if i < 0:
            break

        end = i + L
        left_at_boundary = (i == 0) or (target[i - 1] in _SUBSTR_SEPARATORS)
        right_at_boundary = (end == n) or (target[end] in _SUBSTR_SEPARATORS)

        if left_at_boundary and right_at_boundary:
            bonus = 16
        elif left_at_boundary:
            bonus = 8
        else:
            bonus = 0

        mult = 2 if i > last_slash else 1
        score = (base + bonus) * mult

        if score > best_score:
            best_score = score
            best_pos = i

        # Allow overlapping hits (e.g. "aaa" in "aaaa") so the
        # better-aligned occurrence still gets scored.
        pos = i + 1

    if best_pos < 0:
        return None

    indices = list(range(best_pos, best_pos + L))
    return best_score, indices


def _score_one_token(
    token: str, target: str,
) -> tuple[int, list[int]] | None:
    """Pick the matching strategy for a single token by length.

    Short tokens (≤ 2 chars) are abbreviations and use the subsequence
    scorer. Longer tokens (≥ 3 chars) are treated literally and use the
    substring scorer. The decision is per-token, so a query like
    ``"sp hatred"`` mixes both modes in the same scan.
    """
    if len(token) >= _SUBSTRING_TOKEN_LEN:
        return _substring_token_score(token, target)
    return fuzzy_score(token, target)


def multi_token_score(
    query: str, target: str,
) -> tuple[int, list[int]] | None:
    """Score a whitespace-separated query against ``target``.

    Each token is scored independently with the strategy chosen by
    :func:`_score_one_token` — short tokens stay in subsequence
    (abbreviation) mode, long tokens switch to substring (keyword)
    mode. The decision is per-token so ``"sp hatred"`` does
    subsequence on ``sp`` and substring on ``hatred`` in the same
    pass. AND semantics across tokens: any token that fails to match
    rejects the whole target and returns ``None``.

    Tokens are order-independent — each is scored against the full
    target — so ``"large monster"`` and ``"monster large"`` are
    equivalent.

    The combined score is the sum of per-token scores; the combined
    match indices are the sorted, deduplicated union of every token's
    indices. Substring tokens contribute contiguous ranges (solid
    runs in the highlight delegate); subsequence tokens contribute
    scattered character indices.

    Trailing or leading whitespace is harmless: ``str.split`` drops
    empty entries, so ``"spider"`` and ``"spider "`` are equivalent.
    """
    tokens = query.split()
    if not tokens:
        # Empty/whitespace-only query: trivial match, no highlights.
        # Mirrors fuzzy_score's empty-query contract so callers don't
        # need a separate branch for "user typed only spaces".
        return 0, []
    if len(tokens) == 1:
        # Single-token fast path skips the union machinery — the same
        # mode-selection rule still applies, so a long single token
        # like "spider" goes through substring matching.
        return _score_one_token(tokens[0], target)

    total_score = 0
    union: set[int] = set()
    for tok in tokens:
        res = _score_one_token(tok, target)
        if res is None:
            return None
        score, indices = res
        total_score += score
        union.update(indices)
    return total_score, sorted(union)


if __name__ == "__main__":  # pragma: no cover — manual sanity checks
    cases = [
        # Short single tokens (≤2 chars): subsequence/abbreviation.
        ("ms", "base/meta/Appearance/monster_spider_large.app"),
        ("sp", "base/meta/Appearance/spider_hatred.app"),
        ("xz", "base/meta/Appearance/monster_spider_large.app"),
        # Long single tokens (≥3 chars): substring/keyword. ``Bramble``
        # has b-a-r as a scattered subsequence; substring matching
        # correctly rejects it for both ``bar`` and ``barb``.
        ("bar", "base/meta/Appearance/player_barbarian_male.app"),
        ("bar", "base/meta/Appearance/Bramble_Trap.app"),
        ("barb", "base/meta/Appearance/player_barbarian_male.app"),
        ("barb", "base/meta/Appearance/Bramble_Trap.app"),
        ("spider", "base/meta/Appearance/spider_hatred.app"),
        ("spider", "base/meta/Appearance/goatmanRanged_spearProjectile_impact_distSphere.app"),
        ("shield", "base/meta/Appearance/shield_base06.app"),
        ("monster", "base/meta/Appearance/monster_spider_large.app"),
        # Multi-token, both long: substring AND.
        ("large monster", "base/meta/Appearance/monster_spider_large.app"),
        ("monster large", "base/meta/Appearance/monster_spider_large.app"),
        ("monster zzz", "base/meta/Appearance/monster_spider_large.app"),
        ("spider hatred", "base/meta/Appearance/Hawe_Foliage_Tree.app"),
        ("spider hatred", "base/meta/Appearance/spider_hatred.app"),
        ("spider hatred", "base/meta/Appearance/spider_mini_hatred.app"),
        ("spider hatred", "base/meta/Appearance/SpiderHost_hatred.app"),
        # Mixed mode: short token goes subsequence, long token goes
        # substring, both gates must pass.
        ("sp hatred", "base/meta/Appearance/spider_hatred.app"),
        ("ms barb", "base/meta/Appearance/monster_barbed_spike.app"),
        # Trailing/leading whitespace is dropped by split — these
        # produce identical results to the unwrapped queries above.
        ("spider ", "base/meta/Appearance/spider_hatred.app"),
        (" spider", "base/meta/Appearance/spider_hatred.app"),
    ]
    for q, t in cases:
        result = multi_token_score(q, t)
        if result is None:
            print(f"  {q!r:>16} vs {t}: no match")
        else:
            sc, idxs = result
            highlight = "".join(
                f"[{c}]" if i in idxs else c for i, c in enumerate(t)
            )
            print(f"  {q!r:>16} vs {t}: score={sc}")
            print(f"  {'':>16}    {highlight}")
