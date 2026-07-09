"""Unit tests for the AS_PATH filter + aggregation primitives."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mrt_cn_routes import (  # noqa: E402
    GROUPS,
    T1_ASNS,
    aggregate_prefixes,
    as_path_all_asns,
    is_cn_to_t1_path,
    is_public_prefix,
    normalize_as_path,
    path_contains_any_target,
)


# 1. A CN ASN immediately followed by a T1 ASN must be filtered.
def test_cn_to_t1_adjacent_is_filtered():
    # 10099 (China Unicom Global) -> 6762 (Telecom Italia Sparkle, T1)
    path = [213605, 134823, 10099, 6762, 32934]
    assert is_cn_to_t1_path(path) is True


# 2. Non-adjacent CN and T1 must NOT be filtered on that pair alone.
def test_cn_to_t1_non_adjacent():
    # 10099 and 6762 are NOT adjacent (37963 = Alibaba, not tracked, sits
    # between them and is neither CN nor T1) -> not filtered.
    non_adjacent = [213605, 134823, 10099, 37963, 6762]
    assert is_cn_to_t1_path(non_adjacent) is False

    # The spec's literal example: here 4809 (China Telecom CN2) IS immediately
    # followed by 6762 (T1), so the route IS filtered -- but because of the
    # 4809->6762 adjacency, not the (non-adjacent) 10099->6762 relationship.
    spec_example = [213605, 134823, 10099, 4809, 6762]
    assert is_cn_to_t1_path(spec_example) is True


# 3. A path containing 4134 matches the chinatelecom group.
def test_path_contains_chinatelecom():
    path = [3356, 4134, 65001]
    assert path_contains_any_target(path, GROUPS["chinatelecom"]["asns"]) is True
    # A path without any China Telecom ASN does not match.
    assert path_contains_any_target([3356, 174, 65001], GROUPS["chinatelecom"]["asns"]) is False


# 4. AS_SET counts for membership, but not for adjacency.
def test_as_set_membership_but_not_adjacency():
    # AS_SET containing the target ASN -> membership hit.
    path_with_set = [174, {4134, 4837}, 65010]
    assert path_contains_any_target(path_with_set, GROUPS["chinatelecom"]["asns"]) is True
    assert 4134 in as_path_all_asns(path_with_set)

    # The AS_SET must not create a CN->T1 adjacency: 4837 sits in a SET right
    # next to 6762, but a SET has no ordering so this is NOT a CN->T1 hit.
    path_set_then_t1 = [174, {4837, 4134}, 6762]
    assert is_cn_to_t1_path(path_set_then_t1) is False

    # Sanity: the same ASNs as an ordered SEQUENCE *would* be filtered.
    assert is_cn_to_t1_path([174, 4837, 6762]) is True


# 5. cidr_merge collapses contiguous / aggregatable networks.
#    (Uses real public ranges; documentation/private ranges are now filtered.)
def test_aggregate_prefixes_merges():
    merged = aggregate_prefixes(["1.0.0.0/25", "1.0.0.128/25"])
    assert merged == ["1.0.0.0/24"]

    merged2 = aggregate_prefixes(["116.0.0.0/24", "116.0.1.0/24", "116.0.2.0/24"])
    # 116.0.0.0/24 + 116.0.1.0/24 aggregate to /23; 116.0.2.0/24 stays separate.
    assert "116.0.0.0/23" in merged2
    assert "116.0.2.0/24" in merged2


# 6. IPv4 and IPv6 aggregation stay separate.
def test_v4_v6_separation():
    v4 = aggregate_prefixes(["1.2.3.0/24"])
    v6 = aggregate_prefixes(["2408:8000::/33", "2408:8000:8000::/33"])
    assert v4 == ["1.2.3.0/24"]
    assert v6 == ["2408:8000::/32"]
    # Mixed input still merges within family only.
    mixed = aggregate_prefixes(["1.2.3.0/24", "2408:8000::/32"])
    assert "1.2.3.0/24" in mixed
    assert "2408:8000::/32" in mixed


# Extra: invalid prefixes are skipped, not fatal.
def test_aggregate_skips_invalid():
    merged = aggregate_prefixes(["not-a-prefix", "1.2.3.0/24", ""])
    assert merged == ["1.2.3.0/24"]


# 7. CRITICAL: a default route must never collapse the whole list.
def test_default_route_does_not_swallow_list():
    # Without filtering, cidr_merge([0.0.0.0/0, ...]) would return ['0.0.0.0/0'].
    merged = aggregate_prefixes(["0.0.0.0/0", "1.2.3.0/24", "8.8.8.0/24"])
    assert "0.0.0.0/0" not in merged
    assert set(merged) == {"1.2.3.0/24", "8.8.8.0/24"}

    v6 = aggregate_prefixes(["::/0", "2408:8000::/20", "2400:3200::/32"])
    assert "::/0" not in v6
    assert "2408:8000::/20" in v6


# 8. Bogon / reserved / special-use ranges are dropped (IPv4 and IPv6).
def test_bogons_are_filtered():
    v4_bogons = [
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.1.0/24",
        "192.0.2.0/24", "198.18.0.0/15", "203.0.113.0/24",
        "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    ]
    for b in v4_bogons:
        assert is_public_prefix(b) is False, b
    # Over-broad IPv4 (shorter than /8) is rejected too.
    assert is_public_prefix("0.0.0.0/1") is False
    assert is_public_prefix("128.0.0.0/2") is False
    # Legit public IPv4 kept.
    assert is_public_prefix("1.2.3.0/24") is True
    assert is_public_prefix("116.128.0.0/10") is True  # China Telecom-ish

    v6_bogons = [
        "::/0", "::1/128", "fe80::/10", "fc00::/7", "ff00::/8",
        "2001:db8::/32", "2002::/16", "3ffe::/16", "64:ff9b::/96",
    ]
    for b in v6_bogons:
        assert is_public_prefix(b) is False, b
    # Legit global-unicast IPv6 kept (China Unicom / China Mobile ranges).
    assert is_public_prefix("2408:8000::/20") is True
    assert is_public_prefix("2409:8000::/20") is True


# 9. Aggregation still merges legit prefixes after filtering.
def test_aggregate_after_filtering_still_merges():
    merged = aggregate_prefixes(["10.0.0.0/8", "192.0.2.0/25", "192.0.2.128/25",
                                 "203.0.113.0/24", "1.1.0.0/24", "1.1.1.0/24"])
    # bogons (10/8, 192.0.2.x TEST-NET, 203.0.113 TEST-NET) dropped;
    # 1.1.0.0/24 + 1.1.1.0/24 aggregate to 1.1.0.0/23.
    assert merged == ["1.1.0.0/23"]


# Extra: normalize handles mrtparse-style segment dicts.
def test_normalize_mrtparse_segments():
    raw = [
        {"type": [2, "AS_SEQUENCE"], "value": ["4134", "3356"]},
        {"type": [1, "AS_SET"], "value": ["4837", "9808"]},
    ]
    segs = normalize_as_path(raw)
    assert segs[0].kind == "SEQ"
    assert segs[0].asns == [4134, 3356]
    assert segs[1].kind == "SET"
    assert set(segs[1].asns) == {4837, 9808}
    # 4134 -> 3356 adjacency (3356 = Level3, T1) is detected.
    assert is_cn_to_t1_path(raw) is True
