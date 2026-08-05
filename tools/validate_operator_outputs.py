#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path

# This aggregate is originated through the China Mobile backbone and was the
# concrete cross-operator pollution reported for chinaunicom_v6.txt.
EXCLUSIVE_MOBILE_PROBES = [
    ipaddress.ip_network("2409:8000::/20"),
]

# This prefix is useful as an observation, but not as an exclusivity assertion:
# the sampled RouteViews path can place it behind a verified Unicom p2c chain.
# Rejecting it merely because the organisation is Mobile would contradict the
# requirement to retain real downstream and multihomed customers.
DOWNSTREAM_OBSERVATION_PROBES = [
    ipaddress.ip_network("2409:2000::/31"),
]


def load(path: Path):
    result = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        result.append(ipaddress.ip_network(line, strict=False))
    return result


def overlaps_any(networks, probe):
    return [str(network) for network in networks if network.overlaps(probe)]


def probe_entry(probe, unicom, mobile, baseline_unicom, baseline_mobile, expectation):
    entry = {
        "probe": str(probe),
        "expectation": expectation,
        "generated_unicom_overlaps": overlaps_any(unicom, probe),
        "generated_mobile_overlaps": overlaps_any(mobile, probe),
    }
    if baseline_unicom is not None and baseline_mobile is not None:
        entry.update({
            "baseline_unicom_overlaps": overlaps_any(baseline_unicom, probe),
            "baseline_mobile_overlaps": overlaps_any(baseline_mobile, probe),
        })
    return entry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--report", type=Path, default=Path("validation-report.json"))
    args = parser.parse_args()

    unicom = load(args.output_dir / "chinaunicom_v6.txt")
    mobile = load(args.output_dir / "chinamobile_v6.txt")
    baseline_unicom = (
        load(args.baseline_dir / "chinaunicom_v6.txt") if args.baseline_dir else None
    )
    baseline_mobile = (
        load(args.baseline_dir / "chinamobile_v6.txt") if args.baseline_dir else None
    )

    report = {
        "counts": {
            "generated": {
                "chinaunicom_v6": len(unicom),
                "chinamobile_v6": len(mobile),
            },
            "baseline": {
                "chinaunicom_v6": len(baseline_unicom),
                "chinamobile_v6": len(baseline_mobile),
            } if args.baseline_dir else None,
        },
        "probe_results": [],
        "violations": [],
    }

    for probe in EXCLUSIVE_MOBILE_PROBES:
        entry = probe_entry(
            probe,
            unicom,
            mobile,
            baseline_unicom,
            baseline_mobile,
            "must_be_mobile_not_unicom",
        )
        report["probe_results"].append(entry)
        if entry["generated_unicom_overlaps"]:
            report["violations"].append(
                "China Unicom output still overlaps the exclusive China Mobile "
                f"backbone probe {probe}: {entry['generated_unicom_overlaps']}"
            )
        if not entry["generated_mobile_overlaps"]:
            report["violations"].append(
                f"China Mobile output does not contain expected backbone probe {probe}"
            )

    for probe in DOWNSTREAM_OBSERVATION_PROBES:
        report["probe_results"].append(probe_entry(
            probe,
            unicom,
            mobile,
            baseline_unicom,
            baseline_mobile,
            "observation_only_verified_downstream_may_overlap",
        ))

    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
