from __future__ import annotations

import mrt_cn_routes as m


def _buffers():
    return {key: {"v4": set(), "v6": set()} for key in m.GROUPS}


def _record(prefix: str, path: list[int]) -> m.RouteRecord:
    return m.RouteRecord(prefix, 6 if ":" in prefix else 4, [m.AsPathSegment("SEQ", path)])


def _gates(cn: set[int], p2c: dict[int, set[int]]):
    m._init_gate_globals(p2c, cn)
    gates = m.build_group_gates(m.GROUPS, cn, p2c)
    assert gates is not None
    return gates


def test_nearest_anchor_blocks_cross_operator_absorption():
    # Even if CAIDA models 4837 -> 9808 as p2c, the nearer Mobile anchor wins.
    seq = [174, 4837, 9808, 24445]
    p2c = {4837: {9808}, 9808: {24445}}
    family, index = m.nearest_operator_anchor(seq)
    assert family == "chinamobile"
    assert seq[index] == 9808
    assert m.classify_operator_customer_path(seq, p2c) == "chinamobile"


def test_mobile_customer_does_not_enter_unicom_lists():
    p2c = {4837: {9808}, 9808: {24445}}
    gates = _gates({24445}, p2c)
    buffers = _buffers()
    stats = m.Stats()

    m.flush_route(
        _record("2409:8000::/32", [174, 4837, 9808, 24445]),
        m.GROUPS,
        buffers,
        stats,
        group_gates=gates,
    )

    assert "2409:8000::/32" in buffers["chinamobile"]["v6"]
    assert "2409:8000::/32" in buffers["chinamobile_global"]["v6"]
    assert "2409:8000::/32" in buffers["china_domestic_all"]["v6"]
    assert "2409:8000::/32" in buffers["china_all_global"]["v6"]
    assert "2409:8000::/32" not in buffers["chinaunicom"]["v6"]
    assert "2409:8000::/32" not in buffers["chinaunicom_global"]["v6"]


def test_verified_unicom_downstream_is_retained_without_hardcoding_customer():
    customer_asn = 17621
    p2c = {4837: {customer_asn}}
    gates = _gates({customer_asn}, p2c)
    buffers = _buffers()

    m.flush_route(
        _record("2408:8000::/32", [174, 4837, customer_asn]),
        m.GROUPS,
        buffers,
        m.Stats(),
        group_gates=gates,
    )

    assert "2408:8000::/32" in buffers["chinaunicom"]["v6"]
    assert "2408:8000::/32" in buffers["chinaunicom_global"]["v6"]


def test_unknown_relationship_fails_closed_even_for_cn_origin():
    customer_asn = 64500
    # Exercise the strict runtime gate with a non-empty but unrelated topology.
    p2c = {4134: {64501}}
    gates = _gates({customer_asn}, p2c)
    buffers = _buffers()
    stats = m.Stats()

    m.flush_route(
        _record("2001:db9::/48", [174, 4837, customer_asn]),
        m.GROUPS,
        buffers,
        stats,
        group_gates=gates,
    )

    assert all("2001:db9::/48" not in values["v6"] for values in buffers.values())
    assert stats.total_filtered_foreign_origin == 1


def test_multihomed_customer_can_appear_in_both_from_distinct_paths():
    customer_asn = 64500
    p2c = {4837: {customer_asn}, 9808: {customer_asn}}
    gates = _gates({customer_asn}, p2c)
    buffers = _buffers()
    prefix = "2400:ffff::/48"

    for path in ([174, 4837, customer_asn], [1299, 9808, customer_asn]):
        m.flush_route(
            _record(prefix, list(path)),
            m.GROUPS,
            buffers,
            m.Stats(),
            group_gates=gates,
        )

    assert prefix in buffers["chinaunicom"]["v6"]
    assert prefix in buffers["chinamobile"]["v6"]


def test_trailing_as_set_is_rejected_as_ambiguous():
    p2c = {4837: {64500}}
    gates = _gates({64500, 64501}, p2c)
    buffers = _buffers()
    record = m.RouteRecord(
        "2400:abcd::/48",
        6,
        [m.AsPathSegment("SEQ", [174, 4837]), m.AsPathSegment("SET", [64500, 64501])],
    )
    m.flush_route(record, m.GROUPS, buffers, m.Stats(), group_gates=gates)
    assert all("2400:abcd::/48" not in values["v6"] for values in buffers.values())


def test_as_set_between_anchor_and_origin_is_rejected():
    p2c = {4837: {64500}, 64500: {64501}}
    gates = _gates({64501}, p2c)
    buffers = _buffers()
    record = m.RouteRecord(
        "2400:abce::/48",
        6,
        [
            m.AsPathSegment("SEQ", [174, 4837]),
            m.AsPathSegment("SET", [64500, 64502]),
            m.AsPathSegment("SEQ", [64501]),
        ],
    )
    m.flush_route(record, m.GROUPS, buffers, m.Stats(), group_gates=gates)
    assert all("2400:abce::/48" not in values["v6"] for values in buffers.values())


def test_disabled_gate_never_falls_back_to_path_membership():
    cn = {17621}
    m._init_gate_globals({}, cn)
    gates = m.build_group_gates(m.GROUPS, cn, None)
    assert gates is not None
    buffers = _buffers()
    prefix = "2408:ffff::/48"
    m.flush_route(
        _record(prefix, [174, 4837, 17621]),
        m.GROUPS,
        buffers,
        m.Stats(),
        group_gates=gates,
    )
    assert all(prefix not in values["v6"] for values in buffers.values())
