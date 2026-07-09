#!/usr/bin/env python3
"""Generate China-related ASN CIDR aggregation lists from public MRT data.

This script replaces the old ``birdc``/BIRD based exporter. Instead of reading
the local BIRD routing table, it downloads MRT dumps from public route
collectors (RouteViews, RIPE RIS, PCH, Isolario), extracts every route's
AS_PATH, keeps the routes whose AS_PATH contains any target ASN of a group,
drops routes where a Chinese ASN is immediately followed by a Tier-1 ASN in the
AS_PATH (the "CN -> T1" adjacency filter), and finally aggregates the surviving
prefixes into minimal CIDR lists per group, split into IPv4 and IPv6.

Why the CN -> T1 adjacency filter runs in Python
------------------------------------------------
BIRD could express path filters in its own filter language against the live
table. With MRT dumps we no longer have BIRD, so the same semantics are
re-implemented here in Python: we walk each AS_SEQUENCE and look for an adjacent
pair ``(cn_asn, t1_asn)``. This has to be done on the *ordered* AS_SEQUENCE
only -- AS_SET segments have no internal ordering and therefore must not take
part in the adjacency check (they may still count for "does the path contain an
ASN" membership tests).
"""

from __future__ import annotations

import argparse
import bz2
import concurrent.futures
import gzip
import ipaddress
import json
import logging
import lzma
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import requests
import yaml
from netaddr import IPNetwork, cidr_merge

try:  # tqdm is optional; fall back to a no-op iterator wrapper.
    from tqdm import tqdm
except Exception:  # pragma: no cover - optional dependency
    def tqdm(iterable=None, **_kwargs):  # type: ignore
        return iterable if iterable is not None else []


LOG = logging.getLogger("mrt_cn_routes")

USER_AGENT = "mrt-cn-routes/1.0 (+https://cira.moedove.com)"
AS_TRANS = 23456  # placeholder ASN used when a 4-byte ASN cannot be represented

# ---------------------------------------------------------------------------
# I. ASN configuration (migrated verbatim from the old birdc script)
# ---------------------------------------------------------------------------

ASN_MAP = {
    4134: "China Telecom Backbone",
    4809: "China Telecom CN2",
    23764: "China Telecom Global (CTG)",
    4837: "China Unicom Backbone",
    9929: "China Unicom Industrial Internet Backbone",
    10099: "China Unicom Global",
    9808: "China Mobile Backbone",
    58453: "China Mobile International (CMI)",
    58807: "China Mobile International - NII (CMIN2)",
    268862: "China Mobile International - Brazil",
    137872: "China Mobile Hong Kong",
    209141: "China Mobile International - Russia",
    9231: "China Mobile Hong Kong",
    135054: "China Mobile Group Hainan",
    328787: "China Mobile International - South Africa",
    132389: "China Mobile International - Oceania",
    139619: "China Mobile International - Malaysia",
    141419: "China Mobile International - Thailand",
    4538: "CERNET Backbone",
    23911: "CERNET2 Backbone",
    7497: "CSTNET (Science & Tech)",
    146762: "NATIONAL(SHANGHAI) NEW-TYPE INTERNET EXCHANGE POINT",
}

# ASNs hidden only in the output header. They still participate in matching and
# in the CN -> T1 adjacency filter.
HIDDEN_ASNS = {146762}

# Tier-1 transit ASNs. A CN ASN immediately followed by one of these means the
# route left China through a Tier-1 and is dropped.
T1_ASNS = {
    174, 701, 702, 1239, 1299, 2914, 3257, 3320, 3356,
    3491, 5511, 6453, 6461, 6762, 7018,
}

GROUPS = {
    "china_all": {
        "name": "China All (Domestic + Global + Edu)",
        "asns": list(ASN_MAP.keys()),
    },
    "china_domestic_backbone": {
        "name": "China Domestic Backbone (No Global)",
        "asns": [4134, 4837, 9929, 9808, 4538, 23911, 7497, 146762],
    },
    "chinatelecom": {
        "name": "China Telecom",
        "asns": [4134],
    },
    "chinatelecom_global": {
        "name": "China Telecom (Incl. Global)",
        "asns": [4134, 4809, 23764],
    },
    "chinaunicom": {
        "name": "China Unicom",
        "asns": [4837, 9929],
    },
    "chinaunicom_global": {
        "name": "China Unicom (Incl. Global)",
        "asns": [4837, 9929, 10099],
    },
    "chinamobile": {
        "name": "China Mobile",
        "asns": [9808],
    },
    "chinamobile_global": {
        "name": "China Mobile (Incl. Global)",
        "asns": [9808, 58453, 58807, 268862, 137872, 209141, 9231, 135054, 328787, 132389, 139619, 141419],
    },
    "cernet_edu": {
        "name": "Education & Research Network",
        "asns": [4538, 23911, 7497],
    },
}

# The set of Chinese ASNs used as the *left* side of the CN -> T1 adjacency
# filter. Semantically this is "any Chinese carrier ASN we track", i.e. all of
# ASN_MAP. Kept as a dedicated name so it can be overridden independently of the
# matching groups if the semantics ever need to diverge.
CN_PATH_FILTER_ASNS = set(ASN_MAP.keys())

# Union of every ASN that can make a route match a group. A route whose AS_PATH
# contains none of these cannot match anything, so it is a safe cheap-reject.
ALL_TARGET_ASNS = set().union(*(set(g["asns"]) for g in GROUPS.values()))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MrtFile:
    """A single MRT dump discovered by a provider."""

    source: str
    collector: str
    dump_type: str  # "rib" or "update"
    timestamp: datetime
    url: str
    compression: str = "none"  # bz2 / gz / xz / none
    priority: int = 100  # lower = processed first
    local_path: Optional[Path] = None
    # Fallback URLs (e.g. previous days/dump-periods) tried in order if the
    # primary url is missing. Lets us skip slow HEAD probes entirely.
    alt_urls: list = field(default_factory=list)


@dataclass
class AsPathSegment:
    """A single AS_PATH segment.

    kind is one of: SEQ, SET, CONFED_SEQ, CONFED_SET.
    Order is preserved for SEQ (used by the adjacency filter).
    """

    kind: str
    asns: list[int]


@dataclass
class RouteRecord:
    prefix: str
    ip_version: int
    as_path: list[AsPathSegment]


@dataclass
class Stats:
    processed_files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_raw_routes_seen: int = 0
    total_matched_routes: int = 0
    total_filtered_cn_to_t1: int = 0
    parse_errors: int = 0
    invalid_prefixes: int = 0

    def warn(self, message: str) -> None:
        LOG.warning(message)
        self.warnings.append(message)


# ---------------------------------------------------------------------------
# II. AS_PATH normalization + filter primitives (unit tested)
# ---------------------------------------------------------------------------

# mrtparse AS_PATH segment type codes.
_SEG_TYPE_BY_CODE = {1: "SET", 2: "SEQ", 3: "CONFED_SEQ", 4: "CONFED_SET"}
_SEG_TYPE_BY_NAME = {
    "AS_SET": "SET",
    "AS_SEQUENCE": "SEQ",
    "AS_CONFED_SEQUENCE": "CONFED_SEQ",
    "AS_CONFED_SET": "CONFED_SET",
}


def _coerce_asn(token) -> Optional[int]:
    """Best-effort conversion of a single AS_PATH token into an int."""
    if isinstance(token, bool):  # bool is a subclass of int; reject it.
        return None
    if isinstance(token, int):
        return token
    if isinstance(token, str):
        t = token.strip().strip("{}").strip()
        if not t:
            return None
        # asdot notation "1.10" -> 1 * 65536 + 10
        if "." in t:
            try:
                hi, lo = t.split(".", 1)
                return int(hi) * 65536 + int(lo)
            except ValueError:
                return None
        try:
            return int(t)
        except ValueError:
            return None
    return None


def _mrt_field_code(field) -> Optional[int]:
    """Extract the numeric code from an mrtparse type/subtype field.

    mrtparse 2.x represents these as a single-item dict ``{13: 'TABLE_DUMP_V2'}``;
    older code / our own tests may use ``[13, 'TABLE_DUMP_V2']`` or a bare int.
    """
    if isinstance(field, dict):
        for k in field:
            try:
                return int(k)
            except (TypeError, ValueError):
                return None
    if isinstance(field, (list, tuple)) and field:
        return field[0] if isinstance(field[0], int) else None
    if isinstance(field, int):
        return field
    return None


def _mrt_field_name(field):
    """Extract the human name from an mrtparse type/subtype field."""
    if isinstance(field, dict):
        for v in field.values():
            return v
        return None
    if isinstance(field, (list, tuple)) and len(field) > 1:
        return field[1]
    if isinstance(field, str):
        return field
    return None


def _segment_kind(raw_type) -> str:
    """Resolve a segment kind from a variety of representations."""
    code = _mrt_field_code(raw_type)
    if code in _SEG_TYPE_BY_CODE:
        return _SEG_TYPE_BY_CODE[code]
    name = _mrt_field_name(raw_type)
    if isinstance(name, str) and name in _SEG_TYPE_BY_NAME:
        return _SEG_TYPE_BY_NAME[name]
    if isinstance(raw_type, (list, tuple)):
        for part in raw_type:
            if isinstance(part, str) and part in _SEG_TYPE_BY_NAME:
                return _SEG_TYPE_BY_NAME[part]
    return "SEQ"


def normalize_as_path(raw) -> list[AsPathSegment]:
    """Normalize many possible AS_PATH representations into a segment list.

    Accepts:
      * a flat list/tuple of scalars (ints/strings)     -> one SEQ segment
      * a list mixing scalars, sets, lists and dicts    -> multiple segments
      * mrtparse's AS_PATH attribute value (list of dicts with type/value)
      * an already-normalized list[AsPathSegment]        -> returned as-is

    A ``set`` element becomes an AS_SET segment (and breaks the surrounding
    sequence). A ``list`` element becomes its own AS_SEQUENCE segment. A dict
    with ``type``/``value`` keys is interpreted the mrtparse way.
    """
    if raw is None:
        return []
    if isinstance(raw, AsPathSegment):
        return [raw]
    if isinstance(raw, (str, int)):
        asn = _coerce_asn(raw)
        return [AsPathSegment("SEQ", [asn])] if asn is not None else []
    if isinstance(raw, (set, frozenset)):
        asns = [a for a in (_coerce_asn(x) for x in raw) if a is not None]
        return [AsPathSegment("SET", asns)] if asns else []

    # Already normalized?
    if isinstance(raw, list) and raw and all(isinstance(x, AsPathSegment) for x in raw):
        return raw

    segments: list[AsPathSegment] = []
    seq_buffer: list[int] = []

    def flush_seq() -> None:
        if seq_buffer:
            segments.append(AsPathSegment("SEQ", seq_buffer.copy()))
            seq_buffer.clear()

    for element in raw:
        if isinstance(element, AsPathSegment):
            flush_seq()
            segments.append(element)
        elif isinstance(element, dict) and ("value" in element or "type" in element):
            flush_seq()
            kind = _segment_kind(element.get("type"))
            values = element.get("value", []) or []
            asns = [a for a in (_coerce_asn(x) for x in values) if a is not None]
            if asns:
                segments.append(AsPathSegment(kind, asns))
        elif isinstance(element, (set, frozenset)):
            flush_seq()
            asns = [a for a in (_coerce_asn(x) for x in element) if a is not None]
            if asns:
                segments.append(AsPathSegment("SET", asns))
        elif isinstance(element, (list, tuple)):
            flush_seq()
            asns = [a for a in (_coerce_asn(x) for x in element) if a is not None]
            if asns:
                segments.append(AsPathSegment("SEQ", asns))
        else:
            asn = _coerce_asn(element)
            if asn is not None:
                seq_buffer.append(asn)

    flush_seq()
    return segments


def merge_as4_path(as_path: list[AsPathSegment], as4_path: list[AsPathSegment]) -> list[AsPathSegment]:
    """Merge AS_PATH and AS4_PATH per RFC 6793 (tail replacement).

    We prefer 4-byte information: if AS4_PATH is present and no longer than
    AS_PATH, the trailing ASNs of AS_PATH are replaced by AS4_PATH. Segments are
    flattened to a token stream (ints for SEQ, frozensets for SET) so the
    count-based replacement is straightforward, then rebuilt into segments.
    """
    if not as4_path:
        return as_path
    if not as_path:
        return as4_path

    def flatten(segs: list[AsPathSegment]):
        tokens = []
        for seg in segs:
            if seg.kind in ("SET", "CONFED_SET"):
                tokens.append(("SET", frozenset(seg.asns)))
            else:
                for asn in seg.asns:
                    tokens.append(("SEQ", asn))
        return tokens

    a = flatten(as_path)
    b = flatten(as4_path)
    if len(b) > len(a):
        # Malformed / longer AS4_PATH: keep AS_PATH untouched.
        return as_path
    merged = a[: len(a) - len(b)] + b

    # Rebuild segments, coalescing consecutive SEQ tokens.
    rebuilt: list[AsPathSegment] = []
    seq_buf: list[int] = []
    for kind, value in merged:
        if kind == "SEQ":
            seq_buf.append(value)
        else:
            if seq_buf:
                rebuilt.append(AsPathSegment("SEQ", seq_buf.copy()))
                seq_buf.clear()
            rebuilt.append(AsPathSegment("SET", list(value)))
    if seq_buf:
        rebuilt.append(AsPathSegment("SEQ", seq_buf))
    return rebuilt


def as_path_all_asns(as_path) -> set[int]:
    """All ASNs appearing in SEQ or SET segments (confed segments ignored)."""
    segments = normalize_as_path(as_path)
    result: set[int] = set()
    for seg in segments:
        if seg.kind in ("SEQ", "SET"):
            result.update(seg.asns)
    return result


def path_contains_any_target(as_path, target_asns: Iterable[int]) -> bool:
    """True if the AS_PATH contains any ASN in ``target_asns``.

    Membership considers both AS_SEQUENCE and AS_SET (an AS_SET legitimately
    means "the path went through one of these"), but never confederation
    segments.
    """
    targets = set(target_asns)
    if not targets:
        return False
    return not targets.isdisjoint(as_path_all_asns(as_path))


def is_cn_to_t1_path(
    as_path,
    cn_asns: Iterable[int] = CN_PATH_FILTER_ASNS,
    t1_asns: Iterable[int] = T1_ASNS,
) -> bool:
    """True if a CN ASN is immediately followed by a T1 ASN in any AS_SEQUENCE.

    Only ordered AS_SEQUENCE segments are inspected. AS_SET and confederation
    segments carry no ordering and therefore cannot establish adjacency; an
    AS_SET also breaks adjacency across a sequence boundary because the segments
    are distinct.
    """
    cn = set(cn_asns)
    t1 = set(t1_asns)
    for seg in normalize_as_path(as_path):
        if seg.kind != "SEQ":
            continue
        asns = seg.asns
        for i in range(len(asns) - 1):
            if asns[i] in cn and asns[i + 1] in t1:
                return True
    return False


def aggregate_prefixes(prefixes: Iterable[str]) -> list[str]:
    """Merge a list of CIDR strings into a minimal aggregated list.

    Invalid prefixes are skipped. The result is sorted by network address.
    """
    networks = []
    for p in prefixes:
        try:
            networks.append(IPNetwork(p))
        except Exception:
            continue
    merged = cidr_merge(networks)
    merged.sort()
    return [str(n) for n in merged]


# ---------------------------------------------------------------------------
# III. MRT parsing (streaming, via mrtparse)
# ---------------------------------------------------------------------------

def _extract_paths_from_attributes(path_attributes) -> list[AsPathSegment]:
    """Pull AS_PATH (and AS4_PATH) out of an mrtparse path_attributes list."""
    as_path_raw = None
    as4_path_raw = None
    for attr in path_attributes or []:
        code = _mrt_field_code(attr.get("type"))
        if code == 2:  # AS_PATH
            as_path_raw = attr.get("value")
        elif code == 17:  # AS4_PATH
            as4_path_raw = attr.get("value")
    as_path = normalize_as_path(as_path_raw) if as_path_raw is not None else []
    if as4_path_raw is not None:
        as_path = merge_as4_path(as_path, normalize_as_path(as4_path_raw))
    return as_path


def _ip_version_of(prefix: str) -> int:
    return 6 if ":" in prefix else 4


# --- Fast external parser (bgpdump) ---------------------------------------
# Pure-Python mrtparse is correct but slow (a full RouteViews RIB has tens of
# millions of entries). bgpdump is a C tool that streams the same data ~10-50x
# faster, so we use it automatically when available.

EXTERNAL_PARSERS = ("bgpdump",)


def find_external_parser(preference: str = "auto") -> Optional[str]:
    """Return the path to a usable external MRT parser, or None.

    preference: "auto" (use bgpdump if present), "bgpdump", or "mrtparse"
    (force the pure-Python parser -> returns None).
    """
    if preference == "mrtparse":
        return None
    candidates = [preference] if preference in EXTERNAL_PARSERS else list(EXTERNAL_PARSERS)
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


def _parse_bgpdump_as_path(text: str):
    """Turn a bgpdump machine-format AS_PATH string into a raw segment list.

    bgpdump prints the path space-separated, with AS_SET rendered as a single
    ``{1,2,3}`` token. Confederation segments (parenthesised) are dropped so
    they cannot influence the CN->T1 adjacency check, matching the mrtparse
    behaviour.
    """
    raw = []
    for token in text.split():
        if "(" in token or ")" in token:
            continue  # confederation segment -> ignore
        if token.startswith("{"):
            inner = token.strip("{}")
            members = {p for p in (x.strip() for x in inner.split(",")) if p}
            if members:
                raw.append(members)
        else:
            raw.append(token)
    return raw


def build_target_asn_grep_pattern(groups: dict = GROUPS) -> str:
    """Build a grep -wE alternation of every ASN that could match a group.

    A route whose AS_PATH contains none of these ASNs cannot match any group and
    would be discarded anyway, so pre-filtering on this pattern (in fast C grep)
    is a safe, large speed-up before the per-record Python work.
    """
    asns = set()
    for g in groups.values():
        asns.update(g["asns"])
    return "(" + "|".join(str(a) for a in sorted(asns)) + ")"


def iter_mrt_records_bgpdump(path: Path, tool: str, allow_updates: bool = False,
                             groups: dict = GROUPS) -> Iterator[RouteRecord]:
    """Stream RouteRecords by piping the MRT file through ``bgpdump -m``.

    bgpdump auto-detects bz2/gz compression and emits one pipe-delimited line
    per entry:
        TABLE_DUMP2|ts|B|peer_ip|peer_as|prefix|as_path|origin|...
        BGP4MP|ts|A|peer_ip|peer_as|prefix|as_path|origin|...   (updates)

    When ``grep`` is available the output is pre-filtered to lines that mention a
    target ASN, so Python only touches the ~6% of records that can actually
    match a group (a ~16x reduction on a full RouteViews RIB).
    """
    grep = shutil.which("grep")
    bgp = subprocess.Popen(
        [tool, "-m", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1 << 20,
    )
    filt = None
    try:
        if grep:
            pattern = build_target_asn_grep_pattern(groups)
            filt = subprocess.Popen(
                [grep, "-wE", pattern],
                stdin=bgp.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1 << 20,
            )
            # Let bgpdump receive SIGPIPE if the consumer stops early.
            if bgp.stdout is not None:
                bgp.stdout.close()
            source = filt.stdout
        else:
            # No grep: decode bgpdump's bytes ourselves.
            source = (line.decode("utf-8", "replace") for line in bgp.stdout)  # type: ignore

        for line in source:
            parts = line.rstrip("\n").split("|")
            if len(parts) < 7:
                continue
            rectype = parts[0]
            if rectype.startswith("TABLE_DUMP"):
                pass  # RIB snapshot entry
            elif rectype in ("BGP4MP", "BGP4MP_ET"):
                if not allow_updates or parts[2] != "A":
                    continue
            else:
                continue
            prefix = parts[5]
            if not prefix:
                continue
            as_path = normalize_as_path(_parse_bgpdump_as_path(parts[6]))
            yield RouteRecord(prefix, _ip_version_of(prefix.split("/")[0]), as_path)
    finally:
        for proc in (filt, bgp):
            if proc is None:
                continue
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            proc.wait()


# --- Native struct parser (dependency-free fast path) ---------------------
# A compact TABLE_DUMP_V2 RIB reader using struct. ~10x faster than mrtparse
# and needs no external tool. It performs the target-ASN cheap-reject inline so
# only records that can match a group are turned into RouteRecords.

_MRT_TABLE_DUMP = 12
_MRT_TABLE_DUMP_V2 = 13
_TD1_SUBTYPES = {1: 4, 2: 6}  # TABLE_DUMP v1 subtype -> ip_version
_TD2_PEER_INDEX_TABLE = 1
# RIB subtype -> ip_version (2/4 = plain, 8/9 = ADD-PATH per RFC 8050)
_TD2_RIB_SUBTYPES = {2: 4, 4: 6, 8: 4, 9: 6}
_TD2_ADDPATH_SUBTYPES = {8, 9}
_BGP_ATTR_AS_PATH = 2
_BGP_ATTR_AS4_PATH = 17
_SEG_AS_SET = 1
_SEG_AS_SEQUENCE = 2


def _open_maybe_compressed(path: Path):
    p = str(path)
    if p.endswith(".bz2"):
        return bz2.open(p, "rb")
    if p.endswith(".gz"):
        return gzip.open(p, "rb")
    if p.endswith(".xz"):
        return lzma.open(p, "rb")
    return open(p, "rb")


def _native_format_prefix(ip_version: int, octets: bytes, prefix_len: int) -> str:
    if ip_version == 4:
        addr = octets.ljust(4, b"\x00")
        return f"{addr[0]}.{addr[1]}.{addr[2]}.{addr[3]}/{prefix_len}"
    addr = octets.ljust(16, b"\x00")
    return f"{ipaddress.IPv6Address(addr).compressed}/{prefix_len}"


def _parse_peer_index_table(body: bytes) -> list[bool]:
    """Return a list of is_as4 flags indexed by peer index."""
    pos = 4  # collector_bgp_id
    view_len = struct.unpack_from(">H", body, pos)[0]
    pos += 2 + view_len
    peer_count = struct.unpack_from(">H", body, pos)[0]
    pos += 2
    peers: list[bool] = []
    for _ in range(peer_count):
        peer_type = body[pos]
        pos += 1 + 4  # type byte + bgp id
        pos += 16 if (peer_type & 0x01) else 4  # peer IP
        is_as4 = bool(peer_type & 0x02)
        pos += 4 if is_as4 else 2  # peer AS
        peers.append(is_as4)
    return peers


def _native_as_segments(attr_value: bytes, asn_size: int):
    """Parse one AS_PATH/AS4_PATH value into (seq_runs, set_members)."""
    seq_runs: list[list[int]] = []
    set_members: list[int] = []
    code = "I" if asn_size == 4 else "H"
    pos, n = 0, len(attr_value)
    while pos + 2 <= n:
        seg_type = attr_value[pos]
        seg_len = attr_value[pos + 1]
        pos += 2
        need = seg_len * asn_size
        if pos + need > n:
            break
        if seg_len:
            vals = list(struct.unpack_from(f">{seg_len}{code}", attr_value, pos))
        else:
            vals = []
        if seg_type == _SEG_AS_SEQUENCE:
            if vals:
                seq_runs.append(vals)
        elif seg_type == _SEG_AS_SET:
            set_members.extend(vals)
        # AS_CONFED_* segments are ignored (never affect matching/adjacency).
        pos += need
    return seq_runs, set_members


def _native_extract_paths(attr_bytes: bytes, is_as4: bool):
    """Walk BGP path attributes, return (seq_runs, set_members) for AS_PATH.

    For 2-byte peers we prefer AS4_PATH (real 4-byte ASNs) when present. This is
    a pragmatic substitution rather than a full RFC 6793 merge; use
    --parser mrtparse if you need the strict merge for legacy 2-byte peers.
    """
    pos, n = 0, len(attr_bytes)
    as_path = None
    as4_path = None
    while pos + 2 <= n:
        flags = attr_bytes[pos]
        type_code = attr_bytes[pos + 1]
        pos += 2
        if flags & 0x10:  # extended length
            if pos + 2 > n:
                break
            alen = struct.unpack_from(">H", attr_bytes, pos)[0]
            pos += 2
        else:
            if pos >= n:
                break
            alen = attr_bytes[pos]
            pos += 1
        if pos + alen > n:
            break
        value = attr_bytes[pos:pos + alen]
        pos += alen
        if type_code == _BGP_ATTR_AS_PATH:
            as_path = _native_as_segments(value, 4 if is_as4 else 2)
        elif type_code == _BGP_ATTR_AS4_PATH:
            as4_path = _native_as_segments(value, 4)
    if not is_as4 and as4_path and (as4_path[0] or as4_path[1]):
        return as4_path
    return as_path if as_path is not None else ([], [])


def _native_matches(seq_runs, set_members, targets: set) -> bool:
    for run in seq_runs:
        if not targets.isdisjoint(run):
            return True
    if set_members and not targets.isdisjoint(set_members):
        return True
    return False


def _native_parse_td1(body: bytes, ip_version: int, target_asns: set) -> Optional[RouteRecord]:
    """Parse a single legacy TABLE_DUMP (v1) record. Peer AS is 2-byte."""
    try:
        pos = 2 + 2  # view + sequence
        alen_addr = 4 if ip_version == 4 else 16
        octets = body[pos:pos + alen_addr]
        pos += alen_addr
        prefix_len = body[pos]
        pos += 1 + 1 + 4  # prefix_len + status + originated_time
        pos += alen_addr  # peer IP (same family as subtype)
        pos += 2  # peer AS (2-byte in TABLE_DUMP v1)
        attr_len = struct.unpack_from(">H", body, pos)[0]
        pos += 2
        attr = body[pos:pos + attr_len]
    except (IndexError, struct.error):
        return None
    seq_runs, set_members = _native_extract_paths(attr, is_as4=False)
    if not seq_runs and not set_members:
        return None
    if not _native_matches(seq_runs, set_members, target_asns):
        return None
    segs = [AsPathSegment("SEQ", r) for r in seq_runs]
    if set_members:
        segs.append(AsPathSegment("SET", set_members))
    return RouteRecord(_native_format_prefix(ip_version, octets, prefix_len), ip_version, segs)


def iter_native_rib(path: Path, target_asns: set = ALL_TARGET_ASNS) -> Iterator[RouteRecord]:
    """Stream matching RouteRecords from a TABLE_DUMP_V2 RIB using struct.

    Only records whose AS_PATH contains a target ASN are yielded (the rest can
    never match a group). The prefix string is formatted lazily, after a match.
    """
    peers: list[bool] = []
    fh = _open_maybe_compressed(path)
    try:
        while True:
            header = fh.read(12)
            if len(header) < 12:
                break
            _ts, mrt_type, subtype, length = struct.unpack(">IHHI", header)
            body = fh.read(length)
            if len(body) < length:
                break

            # --- TABLE_DUMP v1 (older, single record per entry) -------------
            if mrt_type == _MRT_TABLE_DUMP:
                ip_version = _TD1_SUBTYPES.get(subtype)
                if ip_version is None:
                    continue
                rec = _native_parse_td1(body, ip_version, target_asns)
                if rec is not None:
                    yield rec
                continue

            if mrt_type != _MRT_TABLE_DUMP_V2:
                continue
            if subtype == _TD2_PEER_INDEX_TABLE:
                peers = _parse_peer_index_table(body)
                continue
            ip_version = _TD2_RIB_SUBTYPES.get(subtype)
            if ip_version is None:
                continue

            addpath = subtype in _TD2_ADDPATH_SUBTYPES
            pos = 4  # sequence number
            prefix_len = body[pos]
            pos += 1
            pbytes = (prefix_len + 7) // 8
            octets = body[pos:pos + pbytes]
            pos += pbytes
            entry_count = struct.unpack_from(">H", body, pos)[0]
            pos += 2

            prefix_str = None  # formatted lazily on first match
            for _ in range(entry_count):
                peer_idx = struct.unpack_from(">H", body, pos)[0]
                pos += 2 + 4  # peer index + originated time
                if addpath:
                    pos += 4  # Path Identifier (per RIB entry, RFC 8050)
                attr_len = struct.unpack_from(">H", body, pos)[0]
                pos += 2
                attr = body[pos:pos + attr_len]
                pos += attr_len

                is_as4 = peers[peer_idx] if peer_idx < len(peers) else True
                seq_runs, set_members = _native_extract_paths(attr, is_as4)
                if not seq_runs and not set_members:
                    continue
                if not _native_matches(seq_runs, set_members, target_asns):
                    continue
                if prefix_str is None:
                    prefix_str = _native_format_prefix(ip_version, octets, prefix_len)
                segs = [AsPathSegment("SEQ", r) for r in seq_runs]
                if set_members:
                    segs.append(AsPathSegment("SET", set_members))
                yield RouteRecord(prefix_str, ip_version, segs)
    finally:
        fh.close()


def iter_mrt_records(path: Path, allow_updates: bool = False) -> Iterator[RouteRecord]:
    """Stream RouteRecords from an MRT file.

    Handles TABLE_DUMP_V2 RIB entries (IPv4/IPv6, including ADD-PATH subtypes),
    legacy TABLE_DUMP, and -- only when ``allow_updates`` is set -- BGP4MP
    UPDATE announcements. mrtparse reads the file incrementally so memory stays
    bounded.
    """
    from mrtparse import Reader  # imported lazily so --help works without it

    for entry in Reader(str(path)):
        data = getattr(entry, "data", None)
        if not isinstance(data, dict):
            continue
        type_name = _mrt_field_name(data.get("type"))

        try:
            if type_name == "TABLE_DUMP_V2":
                prefix = data.get("prefix")
                # For TABLE_DUMP_V2 RIB records mrtparse stores the prefix
                # length in data['length'] (it overwrites the MRT header length).
                plen = data.get("prefix_length", data.get("length"))
                if prefix is None or plen is None or "rib_entries" not in data:
                    continue
                cidr = f"{prefix}/{plen}"
                ipv = _ip_version_of(prefix)
                for rib in data.get("rib_entries", []) or []:
                    as_path = _extract_paths_from_attributes(rib.get("path_attributes"))
                    yield RouteRecord(cidr, ipv, as_path)
            elif type_name == "TABLE_DUMP":
                prefix = data.get("prefix")
                plen = data.get("prefix_length", data.get("length"))
                if prefix is None or plen is None:
                    continue
                cidr = f"{prefix}/{plen}"
                as_path = _extract_paths_from_attributes(data.get("path_attributes"))
                yield RouteRecord(cidr, _ip_version_of(prefix), as_path)
            elif type_name in ("BGP4MP", "BGP4MP_ET") and allow_updates:
                yield from _iter_bgp4mp_announcements(data)
        except Exception:  # pragma: no cover - defensive per-record guard
            # Never let a single malformed record abort the whole file.
            continue


def _iter_bgp4mp_announcements(data: dict) -> Iterator[RouteRecord]:
    """Best-effort extraction of announced prefixes from a BGP4MP UPDATE."""
    bgp = data.get("bgp_message")
    if not isinstance(bgp, dict):
        return
    if _mrt_field_name(bgp.get("type")) != "UPDATE":
        return
    attrs = bgp.get("path_attributes")
    as_path = _extract_paths_from_attributes(attrs)

    def _nlri_len(nlri):
        # mrtparse stores the NLRI prefix length in 'length'.
        return nlri.get("length", nlri.get("prefix_length")) if isinstance(nlri, dict) else None

    # Announced IPv4 NLRI carried directly in the UPDATE.
    for nlri in bgp.get("nlri", []) or []:
        prefix = nlri.get("prefix") if isinstance(nlri, dict) else None
        plen = _nlri_len(nlri)
        if prefix is not None and plen is not None:
            yield RouteRecord(f"{prefix}/{plen}", _ip_version_of(prefix), as_path)

    # Announced prefixes carried in MP_REACH_NLRI (type 14), typically IPv6.
    for attr in attrs or []:
        if _mrt_field_code(attr.get("type")) != 14:
            continue
        value = attr.get("value") or {}
        for nlri in value.get("nlri", []) or []:
            prefix = nlri.get("prefix") if isinstance(nlri, dict) else None
            plen = _nlri_len(nlri)
            if prefix is not None and plen is not None:
                yield RouteRecord(f"{prefix}/{plen}", _ip_version_of(prefix), as_path)


# ---------------------------------------------------------------------------
# IV. Route matching / flushing into per-group buffers
# ---------------------------------------------------------------------------

def flush_route(
    record: RouteRecord,
    groups: dict,
    group_buffers: dict,
    stats: Stats,
    cn_asns: Iterable[int] = CN_PATH_FILTER_ASNS,
    t1_asns: Iterable[int] = T1_ASNS,
) -> None:
    """Apply matching + CN->T1 filtering for a single route and buffer prefixes.

    * Increments ``total_raw_routes_seen``.
    * Validates the prefix; invalid ones are counted and skipped.
    * Computes the CN->T1 verdict once (it is route-level, not group-level).
    * For every group whose target ASNs appear in the AS_PATH, the prefix is
      appended to that group's v4/v6 buffer unless the route was CN->T1 filtered.
    """
    stats.total_raw_routes_seen += 1

    if not record.as_path:
        return

    try:
        ipaddress.ip_network(record.prefix, strict=False)
    except Exception:
        stats.invalid_prefixes += 1
        return

    all_asns = as_path_all_asns(record.as_path)
    if not all_asns:
        return

    # Which groups does this route match (membership only)?
    matched_groups = [
        key for key, g in groups.items()
        if not set(g["asns"]).isdisjoint(all_asns)
    ]
    if not matched_groups:
        return

    cn_to_t1 = is_cn_to_t1_path(record.as_path, cn_asns, t1_asns)
    if cn_to_t1:
        stats.total_filtered_cn_to_t1 += 1
        return

    stats.total_matched_routes += 1
    version_key = "v4" if record.ip_version == 4 else "v6"
    for key in matched_groups:
        # Buffers are sets: a prefix seen via many peers is stored once.
        group_buffers[key][version_key].add(record.prefix)


# ---------------------------------------------------------------------------
# V. Downloader (retry / timeout / UA / .part temp / layered cache)
# ---------------------------------------------------------------------------

class _CandidateMiss(Exception):
    """A candidate URL returned 4xx (missing) — try the next candidate, no retry."""


class Downloader:
    def __init__(self, cache_dir: Path, timeout: int = 60, retries: int = 4,
                 stats: Optional[Stats] = None, pool_size: int = 32):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries
        self.stats = stats
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        # Enlarge the connection pool so many concurrent download threads don't
        # serialize on a tiny default pool (urllib3 default maxsize is 10).
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def head_ok(self, url: str) -> bool:
        """Check availability with HEAD, falling back to a ranged GET."""
        try:
            r = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            if r.status_code < 400:
                return True
            if r.status_code in (403, 405, 501):  # some servers dislike HEAD
                r = self.session.get(url, timeout=self.timeout, stream=True,
                                     headers={"Range": "bytes=0-0"})
                return r.status_code < 400
        except requests.RequestException:
            return False
        return False

    def _dest_for(self, mrt: MrtFile, url: str) -> Path:
        fname = url.rsplit("/", 1)[-1] or "dump"
        return self.cache_dir / mrt.source / mrt.collector / fname

    def download(self, mrt: MrtFile) -> Optional[Path]:
        """Download an MRT file, trying primary + alt_urls in order.

        Each candidate gets its own cache path; a cached file is reused. A 4xx
        on a candidate means "not this one" — we move to the next without
        retrying. Network errors are retried with exponential backoff.
        Uses a ``.part`` temp file renamed on success (no half-written cache).
        """
        candidates = [mrt.url] + [u for u in mrt.alt_urls if u]
        last_error: Optional[Exception] = None
        for url in candidates:
            dest = self._dest_for(mrt, url)
            if dest.exists() and dest.stat().st_size > 0:
                LOG.debug("cache hit: %s", dest)
                mrt.url = url
                return dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                path = self._download_one(url, dest)
                mrt.url = url  # record which candidate actually worked
                return path
            except _CandidateMiss as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
                continue
        raise RuntimeError(
            f"download failed for source={mrt.source} collector={mrt.collector}: "
            f"tried {len(candidates)} url(s); last error: {last_error}"
        )

    def _download_one(self, url: str, dest: Path) -> Path:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            tmp = None
            try:
                with self.session.get(url, timeout=self.timeout, stream=True) as r:
                    if r.status_code >= 400:
                        # Missing/forbidden -> not retryable; caller tries next candidate.
                        raise _CandidateMiss(f"HTTP {r.status_code} for {url}")
                    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
                    tmp = Path(tmp_name)
                    with os.fdopen(fd, "wb") as fh:
                        for chunk in r.iter_content(chunk_size=1 << 16):
                            if chunk:
                                fh.write(chunk)
                os.replace(tmp, dest)
                return dest
            except _CandidateMiss:
                if tmp is not None and tmp.exists():
                    tmp.unlink(missing_ok=True)
                raise
            except Exception as exc:  # network error -> retry
                last_error = exc
                if tmp is not None and tmp.exists():
                    tmp.unlink(missing_ok=True)
                if attempt < self.retries:
                    _sleep(2 ** attempt)
        raise last_error if last_error else RuntimeError(f"download failed: {url}")


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# VI. Providers
# ---------------------------------------------------------------------------

class BaseMrtProvider:
    name = "base"

    def __init__(self, config: dict, downloader: Downloader, stats: Stats, fail_fast: bool = False):
        self.config = config or {}
        self.downloader = downloader
        self.stats = stats
        self.fail_fast = fail_fast

    def discover_files(self, target_time: datetime, collectors: Optional[list[str]], max_files: int) -> list[MrtFile]:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------
    def _select_collectors(self, requested: Optional[list[str]]) -> list[str]:
        configured = self.config.get("collectors") or []
        if requested:
            if configured:
                return [c for c in requested if c in configured] or requested
            return requested
        return list(configured)

    def _warn(self, message: str) -> None:
        self.stats.warn(f"[{self.name}] {message}")

    def _handle_error(self, message: str) -> None:
        if self.fail_fast:
            raise RuntimeError(f"[{self.name}] {message}")
        self._warn(message)


def _cap(files: list, max_files: int) -> list:
    """Truncate to max_files, or return everything when max_files <= 0."""
    if not max_files or max_files <= 0:
        return files
    return files[:max_files]


def _floor_to_interval(dt: datetime, hours: int) -> datetime:
    """Floor a datetime down to a multiple-of-``hours`` boundary (UTC)."""
    dt = dt.astimezone(timezone.utc)
    total = dt.hour
    floored_hour = (total // hours) * hours
    return dt.replace(hour=floored_hour, minute=0, second=0, microsecond=0)


class RouteViewsProvider(BaseMrtProvider):
    name = "routeviews"
    API_URL = "https://api.routeviews.org/meta/collectors"
    ARCHIVE_BASE = "https://archive.routeviews.org"

    def discover_files(self, target_time, collectors, max_files):
        requested = self._select_collectors(collectors)
        use_api = self.config.get("use_api", True)
        files: list[MrtFile] = []

        if use_api:
            try:
                files = self._discover_via_api(target_time, requested)
            except Exception as exc:
                self._warn(f"metadata API failed ({exc}); falling back to archive URLs")
                files = []

        if not files:
            files = self._discover_via_archive(target_time, requested)

        files.sort(key=lambda f: (f.priority, -f.timestamp.timestamp()))
        return _cap(files, max_files)

    def _discover_via_api(self, target_time, requested) -> list[MrtFile]:
        r = self.downloader.session.get(self.API_URL, timeout=self.downloader.timeout)
        r.raise_for_status()
        payload = r.json()
        # Schema: {"data": {"collectors": {"<name>": {"baseURL": ...,
        #   "dataTypes": {"ribs": {"latestDumpTime": "<epoch>",
        #   "latestDumpFile": "<url>", "dumpPeriod": 7200}}}}}}
        data = payload.get("data") if isinstance(payload, dict) else None
        collectors = (data or {}).get("collectors") if isinstance(data, dict) else None
        if not isinstance(collectors, dict) or not collectors:
            raise RuntimeError("unexpected RouteViews API shape")

        files: list[MrtFile] = []
        matched: set[str] = set()
        for name, meta in collectors.items():
            if requested and name not in requested:
                continue
            if not isinstance(meta, dict):
                continue
            ribs = (meta.get("dataTypes") or {}).get("ribs") or {}
            latest_file = ribs.get("latestDumpFile")
            base_url = meta.get("baseURL") or ""
            latest_dt = None
            try:
                if ribs.get("latestDumpTime"):
                    latest_dt = datetime.fromtimestamp(int(ribs["latestDumpTime"]), tz=timezone.utc)
            except (TypeError, ValueError):
                latest_dt = None

            # For "latest" (target at/after the newest dump) use the API's exact
            # latestDumpFile. For an explicit past --time, build the archive URL
            # for that time from the API-provided baseURL.
            if latest_file and (latest_dt is None or target_time >= latest_dt):
                url, ts = latest_file, (latest_dt or target_time)
            elif base_url:
                period_hours = max(1, int(ribs.get("dumpPeriod", 7200)) // 3600)
                dump_time = _floor_to_interval(target_time, period_hours)
                url = (f"{base_url.rstrip('/')}/{dump_time.strftime('%Y.%m')}/RIBS/"
                       f"rib.{dump_time.strftime('%Y%m%d')}.{dump_time.strftime('%H%M')}.bz2")
                ts = dump_time
            else:
                continue
            matched.add(name)
            files.append(MrtFile(
                source=self.name, collector=name, dump_type="rib",
                timestamp=ts, url=url, compression=_compression_of(url), priority=10,
            ))

        # Requested collectors the API does not know about -> archive fallback.
        if requested:
            for name in requested:
                if name not in matched:
                    self._warn(f"collector '{name}' not in RouteViews API; using archive URL")
                    files.extend(self._discover_via_archive(target_time, [name]))

        if not files:
            raise RuntimeError("API returned no usable RIB entries")
        return files

    def _discover_via_archive(self, target_time, requested) -> list[MrtFile]:
        if not requested:
            requested = ["route-views2"]
        # RouteViews RIB dumps are produced every 2 hours.
        dump_time = _floor_to_interval(target_time, 2)
        files: list[MrtFile] = []
        for collector in requested:
            # Every collector (including route-views2) now lives under its own
            # /{collector}/bgpdata/ path on archive.routeviews.org.
            base = f"{self.ARCHIVE_BASE}/{collector}/bgpdata"
            ym = dump_time.strftime("%Y.%m")
            ymd = dump_time.strftime("%Y%m%d")
            hhmm = dump_time.strftime("%H%M")
            url = f"{base}/{ym}/RIBS/rib.{ymd}.{hhmm}.bz2"
            files.append(MrtFile(
                source=self.name, collector=collector, dump_type="rib",
                timestamp=dump_time, url=url, compression="bz2", priority=20,
            ))
        return files


class RipeRisProvider(BaseMrtProvider):
    name = "ris"
    BASE = "https://data.ris.ripe.net"
    # All active RIS route collectors (used when none are configured).
    ALL_COLLECTORS = [
        "rrc00", "rrc01", "rrc03", "rrc04", "rrc05", "rrc06", "rrc07",
        "rrc10", "rrc11", "rrc12", "rrc13", "rrc14", "rrc15", "rrc16",
        "rrc18", "rrc19", "rrc20", "rrc21", "rrc22", "rrc23", "rrc24",
        "rrc25", "rrc26",
    ]

    def discover_files(self, target_time, collectors, max_files):
        requested = self._select_collectors(collectors)
        if not requested:
            requested = self.ALL_COLLECTORS
        # RIS bview dumps are produced every 8 hours (00:00, 08:00, 16:00 UTC).
        dump_time = _floor_to_interval(target_time, 8)
        files: list[MrtFile] = []
        for collector in requested:
            ym = dump_time.strftime("%Y.%m")
            ymd = dump_time.strftime("%Y%m%d")
            hhmm = dump_time.strftime("%H%M")
            url = f"{self.BASE}/{collector}/{ym}/bview.{ymd}.{hhmm}.gz"
            files.append(MrtFile(
                source=self.name, collector=collector, dump_type="rib",
                timestamp=dump_time, url=url, compression="gz", priority=15,
            ))
        files.sort(key=lambda f: f.collector)
        return _cap(files, max_files)


class PchProvider(BaseMrtProvider):
    name = "pch"
    # Real download root (note the /files/ segment, unlike the directory listing
    # URL). Daily per-collector snapshots live at:
    #   {base}/IPv4_daily_snapshots/{YYYY}/{MM}/{collector}/
    #       {collector}-ipv4_bgp_routes.{YYYY}.{MM}.{DD}.gz
    #   {base}/IPv6_daily_snapshots/{YYYY}/{MM}/{collector}/
    #       {collector}-ipv6_bgp_routes.{YYYY}.{MM}.{DD}.gz
    DEFAULT_BASE = "https://downloads.pch.net/files/Routing_Data"
    # Directory-listing root (note: no /files/ segment, unlike the download root).
    DEFAULT_INDEX_BASE = "https://downloads.pch.net/Routing_Data"
    # Fallback Asia/HK collectors, used only if auto-enumeration fails.
    DEFAULT_COLLECTORS = [
        "route-collector.hkg.pch.net",
        "route-collector.hkg2.pch.net",
        "route-collector.tpe.pch.net",
        "route-collector.equinix-sg.pch.net",
        "route-collector.icn.pch.net",
        "route-collector.nrt.pch.net",
    ]

    def discover_files(self, target_time, collectors, max_files):
        cfg = self.config
        base = cfg.get("base_url") or (cfg.get("base_urls") or [None])[0] or self.DEFAULT_BASE
        want_v6 = cfg.get("ipv6", True)
        lookback = int(cfg.get("date_lookback_days", 2))

        # No explicit collectors -> enumerate ALL current PCH collectors from the
        # monthly directory index (full coverage, always up to date).
        requested = self._select_collectors(collectors)
        if not requested:
            requested = self._enumerate_collectors(base, target_time, lookback) \
                or self.DEFAULT_COLLECTORS

        # PCH daily snapshots ARE full per-collector tables, so they count as RIBs.
        families = [(4, "IPv4", "ipv4")]
        if want_v6:
            families.append((6, "IPv6", "ipv6"))

        # No HEAD probing: build candidate URLs (today, then previous days) and
        # let the download stage pick the first that exists. This makes PCH
        # discovery instant even with hundreds of collectors.
        files: list[MrtFile] = []
        for collector in requested:
            for ip_version, subdir, kind in families:
                urls = [self._snapshot_url(base, collector, subdir, kind,
                                           target_time - timedelta(days=d))
                        for d in range(0, lookback + 1)]
                files.append(MrtFile(
                    source=self.name, collector=f"{collector}/{kind}", dump_type="rib",
                    timestamp=target_time, url=urls[0], compression="gz",
                    priority=40, alt_urls=urls[1:],
                ))
        return _cap(files, max_files)

    def _enumerate_collectors(self, base, target_time, lookback) -> list[str]:
        """List every collector under the monthly snapshot directory.

        Best-effort: scrapes the PCH directory listing with a regex (no external
        HTML parser needed). Returns [] on failure so the caller can fall back.
        """
        import re
        index_base = self.config.get("index_base_url") or self.DEFAULT_INDEX_BASE
        for delta in range(0, lookback + 1):
            dt = (target_time - timedelta(days=delta)).astimezone(timezone.utc)
            url = f"{index_base.rstrip('/')}/IPv4_daily_snapshots/{dt:%Y}/{dt:%m}/"
            try:
                r = self.downloader.session.get(url, timeout=self.downloader.timeout)
                if r.status_code >= 400:
                    continue
                names = sorted(set(re.findall(r"route-collector[A-Za-z0-9._-]*\.pch\.net", r.text)))
                if names:
                    LOG.info("[pch] enumerated %d collectors from %s", len(names), url)
                    return names
            except requests.RequestException as exc:
                self._warn(f"collector index fetch failed {url}: {exc}")
        self._warn("could not enumerate PCH collectors; using built-in default list")
        return []

    @staticmethod
    def _snapshot_url(base, collector, subdir, kind, dt):
        dt = dt.astimezone(timezone.utc)
        return (f"{base.rstrip('/')}/{subdir}_daily_snapshots/"
                f"{dt:%Y}/{dt:%m}/{collector}/"
                f"{collector}-{kind}_bgp_routes.{dt:%Y}.{dt:%m}.{dt:%d}.gz")


PROVIDER_REGISTRY = {
    "routeviews": RouteViewsProvider,
    "ris": RipeRisProvider,
    "pch": PchProvider,
}


# ---------------------------------------------------------------------------
# URL / compression helpers
# ---------------------------------------------------------------------------

def _compression_of(url: str) -> str:
    lower = url.lower()
    if lower.endswith(".bz2"):
        return "bz2"
    if lower.endswith(".gz"):
        return "gz"
    if lower.endswith(".xz"):
        return "xz"
    return "none"


# ---------------------------------------------------------------------------
# VII. Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "sources": {
        "routeviews": {
            "enabled": True,
            "use_api": True,
            "collectors": [
                "route-views2", "route-views3", "route-views4", "route-views6",
                "route-views.eqix", "route-views.sg", "route-views.linx",
            ],
        },
        "ris": {
            "enabled": True,
            "collectors": [
                "rrc00", "rrc01", "rrc03", "rrc04", "rrc10", "rrc11", "rrc12",
                "rrc13", "rrc14", "rrc15", "rrc16", "rrc18", "rrc19", "rrc20",
                "rrc21", "rrc22", "rrc23", "rrc24", "rrc25", "rrc26",
            ],
        },
        "pch": {
            "enabled": True,
            "base_url": "https://downloads.pch.net/files/Routing_Data",
            # Empty -> provider uses its built-in Asia/HK default collector list.
            "collectors": [],
            "ipv6": True,
            "date_lookback_days": 2,
        },
    }
}


def load_config(path: Path) -> dict:
    """Load source config from YAML, falling back to the built-in defaults."""
    if not path.exists():
        LOG.info("source config %s not found; using built-in defaults", path)
        return DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    merged = {"sources": {}}
    default_sources = DEFAULT_CONFIG["sources"]
    user_sources = data.get("sources", {}) or {}
    for name, default in default_sources.items():
        merged_source = dict(default)
        merged_source.update(user_sources.get(name, {}) or {})
        merged["sources"][name] = merged_source
    # Preserve any extra user-defined sources too.
    for name, cfg in user_sources.items():
        if name not in merged["sources"]:
            merged["sources"][name] = cfg
    return merged


# ---------------------------------------------------------------------------
# VIII. Output
# ---------------------------------------------------------------------------

def _visible_asns(asns: Sequence[int]) -> list[int]:
    return [a for a in asns if a not in HIDDEN_ASNS]


def write_group_output(output_dir: Path, group_key: str, group: dict,
                       cidrs: list[str], ip_version: int, generated: str) -> Path:
    """Write one group's aggregated list with the legacy header style."""
    visible = _visible_asns(group["asns"])
    suffix = "v4" if ip_version == 4 else "v6"
    out_path = output_dir / f"{group_key}_{suffix}.txt"
    lines = [
        f"# Group: {group['name']}",
        f"# Key: {group_key}",
        f"# Generated: {generated}",
        f"# Total ASNs: {len(visible)}",
        f"# ASN List: {', '.join(str(a) for a in visible)}",
        "# Details:",
    ]
    for asn in visible:
        lines.append(f"#   {asn}: {ASN_MAP.get(asn, 'Unknown')}")
    lines.append("")
    lines.extend(cidrs)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# IX. Orchestration
# ---------------------------------------------------------------------------

def parse_target_time(value: str) -> datetime:
    if value == "latest":
        return datetime.now(timezone.utc)
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_providers(sources: list[str], config: dict, downloader: Downloader,
                    stats: Stats, fail_fast: bool) -> list[BaseMrtProvider]:
    providers: list[BaseMrtProvider] = []
    source_configs = config.get("sources", {})
    for name in sources:
        cls = PROVIDER_REGISTRY.get(name)
        if cls is None:
            stats.warn(f"unknown source '{name}'; ignored")
            continue
        cfg = source_configs.get(name, {})
        if not cfg.get("enabled", True):
            LOG.info("source %s disabled in config; skipping", name)
            continue
        providers.append(cls(cfg, downloader, stats, fail_fast=fail_fast))
    return providers


def process_file(mrt: MrtFile, groups: dict, group_buffers: dict, stats: Stats,
                 allow_updates: bool, parser_mode: str = "native",
                 external_tool: Optional[str] = None, verbose: bool = False) -> None:
    """Parse a single downloaded MRT file into the group buffers."""
    if mrt.local_path is None:
        return
    process_updates = allow_updates and mrt.dump_type == "update"
    # The native parser only reads RIB dumps; fall back to mrtparse for updates.
    if parser_mode == "bgpdump" and external_tool:
        record_iter = iter_mrt_records_bgpdump(mrt.local_path, external_tool,
                                               allow_updates=process_updates, groups=groups)
    elif parser_mode == "native" and not process_updates:
        record_iter = iter_native_rib(mrt.local_path, ALL_TARGET_ASNS)
    else:
        record_iter = iter_mrt_records(mrt.local_path, allow_updates=process_updates)
    try:
        count = 0
        for record in record_iter:
            flush_route(record, groups, group_buffers, stats)
            count += 1
            if verbose and count % 500000 == 0:
                LOG.debug("  %s/%s: %d records parsed so far", mrt.source, mrt.collector, count)
        LOG.info("parsed %s/%s: %d records", mrt.source, mrt.collector, count)
    except Exception as exc:
        stats.parse_errors += 1
        stats.warn(f"parse error for {mrt.source}/{mrt.collector} {mrt.url}: {exc}")


def _parse_file_worker(task: tuple) -> dict:
    """Top-level worker for the parse ProcessPool. Parses ONE file and returns
    per-group prefix lists + counters (sets become lists so they pickle back).

    Runs in a separate process, so it must be a module-level function and must
    not touch any shared/mutable main-process state.
    """
    mrt, parser_mode, external_tool = task
    buffers = {key: {"v4": set(), "v6": set()} for key in GROUPS}
    stats = Stats()
    process_file(mrt, GROUPS, buffers, stats, allow_updates=False,
                 parser_mode=parser_mode, external_tool=external_tool, verbose=False)
    return {
        "url": mrt.url,
        "buffers": {k: {"v4": list(v["v4"]), "v6": list(v["v6"])} for k, v in buffers.items()},
        "raw": stats.total_raw_routes_seen,
        "matched": stats.total_matched_routes,
        "filtered": stats.total_filtered_cn_to_t1,
        "parse_errors": stats.parse_errors,
        "invalid": stats.invalid_prefixes,
        "warnings": stats.warnings,
    }


def run(args) -> int:
    stats = Stats()
    output_dir = Path(args.output_dir)
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(Path(args.source_config))
    target_time = parse_target_time(args.time)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    collectors = None if args.collectors == "all" else [c.strip() for c in args.collectors.split(",") if c.strip()]

    downloader = Downloader(cache_dir, timeout=args.timeout, stats=stats)
    providers = build_providers(sources, config, downloader, stats, args.fail_fast)

    # --- discovery ------------------------------------------------------
    discovered: list[MrtFile] = []
    for provider in providers:
        try:
            files = provider.discover_files(target_time, collectors, args.max_files_per_source)
            LOG.info("%s: discovered %d file(s)", provider.name, len(files))
            discovered.extend(files)
        except Exception as exc:
            if args.fail_fast:
                raise
            stats.warn(f"[{provider.name}] discovery failed: {exc}")

    if args.dry_run:
        print(f"Dry run: {len(discovered)} MRT file(s) would be processed")
        for mrt in discovered:
            print(f"  [{mrt.source}/{mrt.collector}] {mrt.dump_type} "
                  f"{mrt.timestamp.isoformat()} {mrt.url}")
        if stats.warnings:
            print(f"\nWarnings ({len(stats.warnings)}):")
            for w in stats.warnings:
                print(f"  - {w}")
        return 0

    # --- parser selection ----------------------------------------------
    #   mrtparse -> pure-Python, most compatible (updates, exotic formats)
    #   native   -> built-in struct RIB parser, ~10x mrtparse, no external dep
    #   bgpdump  -> external C tool (+ grep pre-filter), fastest when installed
    #   auto     -> bgpdump if on PATH, else native
    external_tool = None
    if args.parser in ("auto", "bgpdump"):
        external_tool = find_external_parser("bgpdump")
    if args.parser == "mrtparse":
        parser_mode = "mrtparse"
    elif args.parser == "native":
        parser_mode = "native"
    elif args.parser == "bgpdump":
        if external_tool:
            parser_mode = "bgpdump"
        else:
            stats.warn("bgpdump requested but not found on PATH; using native parser")
            parser_mode = "native"
    else:  # auto
        parser_mode = "bgpdump" if external_tool else "native"

    parse_workers = args.parse_workers if args.parse_workers > 0 else (os.cpu_count() or 4)
    if parser_mode == "bgpdump":
        LOG.info("using parser: bgpdump (%s) + grep pre-filter", external_tool)
    else:
        LOG.info("using parser: %s", parser_mode)
    LOG.info("parallelism: %d download thread(s), %d parse process(es)",
             args.parallel_downloads, parse_workers)

    # --- download (threads), then parse (processes, biggest-first) -----
    # Downloads are I/O-bound -> thread pool. Parsing is CPU-bound -> process
    # pool (sidesteps the GIL). We parse LARGEST files first (longest-processing-
    # time scheduling): a few huge RIBs otherwise become end-of-run stragglers
    # that leave most cores idle. Feeding them in first keeps all cores busy and
    # small files fill the tail, minimizing wall-clock.
    def _download(mrt: MrtFile) -> Optional[MrtFile]:
        try:
            mrt.local_path = downloader.download(mrt)
            return mrt
        except Exception as exc:
            if args.fail_fast:
                raise
            stats.warn(str(exc))
            stats.skipped_files.append(mrt.url)
            return None

    group_buffers = {key: {"v4": set(), "v6": set()} for key in GROUPS}

    def _merge(result: dict) -> None:
        for key, vv in result["buffers"].items():
            group_buffers[key]["v4"].update(vv["v4"])
            group_buffers[key]["v6"].update(vv["v6"])
        stats.total_raw_routes_seen += result["raw"]
        stats.total_matched_routes += result["matched"]
        stats.total_filtered_cn_to_t1 += result["filtered"]
        stats.parse_errors += result["parse_errors"]
        stats.invalid_prefixes += result["invalid"]
        stats.warnings.extend(result["warnings"])
        stats.processed_files.append(result["url"])

    ThreadPool = concurrent.futures.ThreadPoolExecutor
    ProcessPool = concurrent.futures.ProcessPoolExecutor

    downloaded: list[MrtFile] = []
    with ThreadPool(max_workers=args.parallel_downloads) as dpool:
        for mrt in tqdm(dpool.map(_download, discovered), total=len(discovered),
                        desc="download", disable=not args.verbose):
            if mrt is not None and mrt.local_path is not None:
                downloaded.append(mrt)

    def _file_size(m: MrtFile) -> int:
        try:
            return m.local_path.stat().st_size
        except OSError:
            return 0

    downloaded.sort(key=_file_size, reverse=True)  # LPT: biggest first

    with ProcessPool(max_workers=parse_workers) as ppool:
        fut_to_mrt = {}
        for mrt in downloaded:
            fut = ppool.submit(_parse_file_worker, (mrt, parser_mode, external_tool))
            fut_to_mrt[fut] = mrt
        for pf in tqdm(concurrent.futures.as_completed(fut_to_mrt), total=len(fut_to_mrt),
                       desc="parse", disable=not args.verbose):
            try:
                _merge(pf.result())
            except Exception as exc:
                stats.parse_errors += 1
                stats.warn(f"parse worker failed: {exc}")
            # Free each cached file as soon as it's parsed (keeps peak disk low,
            # important on CI runners). End-of-run cleanup still runs as a backstop.
            if not args.keep_cache:
                done_mrt = fut_to_mrt.get(pf)
                if done_mrt is not None and done_mrt.local_path is not None:
                    try:
                        done_mrt.local_path.unlink(missing_ok=True)
                    except OSError:
                        pass

    # --- aggregate + write ---------------------------------------------
    output_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    per_group_v4: dict[str, int] = {}
    per_group_v6: dict[str, int] = {}
    for key, group in GROUPS.items():
        v4 = aggregate_prefixes(group_buffers[key]["v4"])
        v6 = aggregate_prefixes(group_buffers[key]["v6"])
        write_group_output(output_dir, key, group, v4, 4, generated)
        write_group_output(output_dir, key, group, v6, 6, generated)
        per_group_v4[key] = len(v4)
        per_group_v6[key] = len(v6)
        LOG.info("group %s: %d v4 / %d v6 aggregated prefixes", key, len(v4), len(v6))

    # --- summary --------------------------------------------------------
    summary = {
        "generated_at": generated,
        "enabled_sources": [p.name for p in providers],
        "processed_files": stats.processed_files,
        "skipped_files": stats.skipped_files,
        "warnings": stats.warnings,
        "per_group_count_v4": per_group_v4,
        "per_group_count_v6": per_group_v6,
        "total_raw_routes_seen": stats.total_raw_routes_seen,
        "total_matched_routes": stats.total_matched_routes,
        "total_filtered_cn_to_t1": stats.total_filtered_cn_to_t1,
        "parse_errors": stats.parse_errors,
        "invalid_prefixes": stats.invalid_prefixes,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.keep_cache:
        _cleanup_cache(downloaded)

    print(json.dumps({
        "generated_at": generated,
        "processed_files": len(stats.processed_files),
        "skipped_files": len(stats.skipped_files),
        "warnings": len(stats.warnings),
        "total_raw_routes_seen": stats.total_raw_routes_seen,
        "total_matched_routes": stats.total_matched_routes,
        "total_filtered_cn_to_t1": stats.total_filtered_cn_to_t1,
    }, indent=2))
    return 0


def _cleanup_cache(downloaded: list[MrtFile]) -> None:
    for mrt in downloaded:
        if mrt.local_path and mrt.local_path.exists():
            try:
                mrt.local_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate China ASN CIDR aggregation lists from public MRT data.",
    )
    p.add_argument("--output-dir", default="/www/wwwroot/cira.moedove.com",
                   help="Directory for the generated {group}_v4.txt / _v6.txt files.")
    p.add_argument("--cache-dir", default="/var/cache/mrt-cn-routes",
                   help="Directory for cached MRT downloads (layered by source/collector/date).")
    p.add_argument("--sources", default="routeviews,ris,pch",
                   help="Comma-separated list of sources to enable.")
    p.add_argument("--source-config", default="./mrt_sources.yml",
                   help="Path to the source config YAML (defaults + example structure used if missing).")
    p.add_argument("--time", default="latest",
                   help="'latest' or an ISO8601 timestamp, e.g. 2026-07-09T00:00:00Z.")
    p.add_argument("--collectors", default="all",
                   help="'all' or a comma-separated list of collectors to restrict to.")
    p.add_argument("--max-files-per-source", type=int, default=0,
                   help="Maximum number of MRT files to fetch per source. "
                        "0 (default) means no limit — fetch every configured collector "
                        "for maximum coverage.")
    p.add_argument("--parallel-downloads", type=int, default=8,
                   help="Number of concurrent downloads (I/O-bound thread pool).")
    p.add_argument("--parse-workers", type=int, default=0,
                   help="Number of parallel parse processes (CPU-bound). "
                        "0 (default) = number of CPU cores.")
    p.add_argument("--timeout", type=int, default=120,
                   help="Per-request timeout in seconds (PCH can be slow to start; "
                        "raise this if PCH HEAD/GET times out).")
    p.add_argument("--parser", choices=["auto", "native", "bgpdump", "mrtparse"], default="auto",
                   help="MRT parser: 'auto' uses bgpdump if available else the built-in "
                        "native struct parser; 'native' forces the dependency-free fast "
                        "parser (~10x mrtparse); 'bgpdump' forces the external C tool; "
                        "'mrtparse' forces pure-Python mrtparse (most compatible).")
    p.add_argument("--dry-run", action="store_true",
                   help="Only list the MRT files that would be downloaded/processed.")
    p.add_argument("--keep-cache", action="store_true",
                   help="Keep downloaded MRT files after processing.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging + progress bars.")
    p.add_argument("--fail-fast", action="store_true",
                   help="Abort on the first source/collector failure instead of warning.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return run(args)
    except KeyboardInterrupt:  # pragma: no cover
        LOG.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
