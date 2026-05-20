"""Tests for the QA-pair allocation helper used by generate_qa_pairs_from_documents.

The helper is the load-bearing piece of "if the user asks for N pairs, give
them N pairs." Integer-truncation would silently drop `num_samples %
len(doc_paths)` pairs, so we test that remainder distribution is exact.
"""
from eval_mcp.tools.generate_qa import _allocate_pairs


def test_remainder_distributes_to_match_total_exactly():
    # 1000 across 51 docs: the case that prompted this fix. Naive `// len`
    # would give 19*51 = 969, losing 31 pairs.
    allocs = _allocate_pairs(1000, 51)
    assert sum(allocs) == 1000
    assert max(allocs) - min(allocs) <= 1  # at most one off in any bin


def test_equal_split_when_total_divides_evenly():
    assert _allocate_pairs(100, 10) == [10] * 10


def test_fewer_pairs_than_bins_gives_zero_tail():
    # 3 pairs across 10 docs: first three each get 1, rest get 0. A zero
    # allocation means "skip this doc" in the caller; verify the helper
    # gives a clean signal.
    allocs = _allocate_pairs(3, 10)
    assert allocs == [1, 1, 1, 0, 0, 0, 0, 0, 0, 0]


def test_zero_bins_returns_empty():
    assert _allocate_pairs(100, 0) == []


def test_single_bin_takes_everything():
    assert _allocate_pairs(1000, 1) == [1000]


def test_zero_total_gives_zero_per_bin():
    assert _allocate_pairs(0, 5) == [0, 0, 0, 0, 0]
