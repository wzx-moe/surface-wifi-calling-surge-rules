#!/usr/bin/env python3
"""Offline, conservative post-processing of the public country Wi-Fi rules.

Run after the upstream generator and before its normal publication step.
Default: preview only. --apply writes. --check exits 1 if changes are required.
No network, Git operation, private profile, subscription, or credentials are used.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

BASE = Path("outputs/surge_wifi_calling_country_carriers/v5")
MIN_BASELINE_RETENTION_PCT = 90
BEGIN = "# BEGIN managed WiFi GeoIP fallbacks"
END = "# END managed WiFi GeoIP fallbacks"
ADD_COMMENT = "# Verified additions maintained by maintenance/wifi_rules/registry.json"
SUMMARY_NOTE = "# Formal country rules; GeoIP compatibility rules are counted separately in legacy_geoip/SUMMARY.txt."


class InvalidInput(ValueError):
    pass


def active(line: str) -> str:
    value = line.strip().lstrip("\ufeff")
    return "" if not value or value.startswith(("#", ";", "//")) else value


def rules(text: str) -> list[str]:
    return [value for line in text.splitlines() if (value := active(line))]


def key(value: str) -> str:
    # Normalize only the known simple signatures. Whitespace in a future regex
    # or other rule payload may be meaningful and must not hide aggregate loss.
    candidate = re.sub(r"\s+", "", value)
    if re.fullmatch(r"AND,\(\(PROTOCOL,UDP\),\(DEST-PORT,(?:500|4500)\),\(GEOIP,[A-Z]{2}\)\)", candidate):
        return candidate
    if re.fullmatch(r"DOMAIN(?:-SUFFIX)?\s*,\s*[a-zA-Z0-9.-]+\s*", value):
        return ",".join(part.strip() for part in value.split(","))
    if re.fullmatch(r"IP-CIDR6?\s*,\s*[0-9a-fA-F:./]+(?:\s*,\s*no-resolve)?\s*", value):
        return ",".join(part.strip() for part in value.split(","))
    return value.strip()


def newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def append_lines(text: str, extra: list[str]) -> str:
    if not extra:
        return text
    nl = newline(text)
    if text and not text.endswith(("\r", "\n")):
        text += nl
    return text + nl.join(extra) + nl


def load_contract(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("countries"), list):
        raise InvalidInput("Unsupported registry schema")
    seen = set()
    for item in data["countries"]:
        cc = item["cc"]
        if not re.fullmatch(r"[A-Z]{2}", cc) or cc in seen:
            raise InvalidInput("Invalid or duplicate registry country")
        seen.add(cc)
        required = item["required_rules"]
        if any(not re.fullmatch(r"DOMAIN(?:-SUFFIX)?,[a-z0-9.-]+", s) for s in required):
            raise InvalidInput(f"Unexpected supplemental syntax for {cc}")
        expected = [f"AND,((PROTOCOL,UDP),(DEST-PORT,{port}),(GEOIP,{cc}))" for port in (500, 4500)]
        if sorted(map(key, item["geoip_fallback"])) != sorted(map(key, expected)):
            raise InvalidInput(f"Unexpected GeoIP contract for {cc}")
    return data


def summary(text: str, counts: dict[str, int], note: str) -> str:
    """Keep existing row order and other text; change counts and add new rows."""
    nl = newline(text)
    seen = set()
    output = []
    total_position = None
    for line in text.splitlines(keepends=True):
        match = re.match(r"^(\w+)(: rules=)(\d+)([^\r\n]*)(\r?\n|\r|$)$", line)
        if not match:
            output.append(line)
            continue
        cc = match.group(1)
        if cc in seen:
            raise InvalidInput(f"Duplicate SUMMARY entry: {cc}")
        seen.add(cc)
        if cc == "TOTAL":
            total_position = len(output)
            number = sum(counts.values())
        elif cc in counts:
            number = counts[cc]
        else:
            raise InvalidInput(f"SUMMARY references a missing country file: {cc}")
        output.append(f"{cc}{match.group(2)}{number}{match.group(4)}{match.group(5) or nl}")
    missing = [f"{cc}: rules={count}{nl}" for cc, count in counts.items() if cc not in seen]
    if total_position is None:
        if output and not output[-1].endswith(("\r", "\n")):
            output[-1] += nl
        output.extend(missing)
        output.append(f"TOTAL: rules={sum(counts.values())}{nl}")
    else:
        output[total_position:total_position] = missing
    combined = "".join(output)
    if note not in combined:
        combined = note + nl + combined
    # This is the original public header; update its now-inaccurate wording only.
    combined = combined.replace(
        "# Non-comment Surge rule counts, including real-IP fallbacks and carrier-specific entries.",
        "# Non-comment Surge formal rule counts, including carrier-specific entries."
    )
    return combined


def patch_template(text: str, countries: set[str], label: str) -> str:
    """Preserve public template text except one managed block after country rules."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, s in enumerate(lines) if s.strip() == BEGIN]
    ends = [i for i, s in enumerate(lines) if s.strip() == END]
    previous_block = None
    previous_start = None
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise InvalidInput(f"Ambiguous managed block in {label}")
        prior_sections = [active(s).lower() for s in lines[:starts[0]] if active(s).startswith("[")]
        if not prior_sections or prior_sections[-1] != "[rule]":
            raise InvalidInput(f"Managed block is outside [Rule] in {label}")
        previous_start = starts[0]
        previous_block = [value for s in lines[starts[0] + 1:ends[0]] if (value := active(s))]
        del lines[starts[0]:ends[0] + 1]
    section = None
    country_rows = []
    seen = set()
    for index, line in enumerate(lines):
        clean = active(line)
        if clean.startswith("["):
            section = clean.lower()
        if section != "[rule]" or not clean:
            continue
        if "/legacy_geoip/" in clean or clean.startswith("RULE-SET,legacy_geoip/"):
            raise InvalidInput(f"Unmanaged legacy references in {label}; review their placement first")
        parts = [part.strip() for part in clean.split(",")]
        if len(parts) < 3 or parts[0] != "RULE-SET":
            continue
        match = re.search(r"(?:^|/)rulesets/WiFiCalling-([A-Z]{2})\.list$", parts[1])
        if not match:
            continue
        cc = match.group(1)
        if cc in seen:
            raise InvalidInput(f"Duplicate country {cc} in {label}")
        seen.add(cc)
        old = f"rulesets/WiFiCalling-{cc}.list"
        new = f"legacy_geoip/WiFiCalling-{cc}-GeoIP.list"
        country_rows.append((index, cc, parts[1][:-len(old)] + new, parts[2]))
    if seen != countries:
        raise InvalidInput(f"Country RULE-SET coverage differs from registry in {label}: missing={sorted(countries-seen)}, extra={sorted(seen-countries)}")
    first, last = country_rows[0][0], country_rows[-1][0]
    country_positions = {row[0] for row in country_rows}
    for index in range(first, last + 1):
        if active(lines[index]) and index not in country_positions:
            raise InvalidInput(f"Non-country rule interrupts the precise country block in {label}")
    if any(active(s).startswith("FINAL,") for s in lines[:last + 1]):
        raise InvalidInput(f"FINAL precedes country rules in {label}")
    expected_block = [f"RULE-SET,{url},{policy},no-resolve" for _, _, url, policy in country_rows]
    if previous_block is not None:
        if collections.Counter(previous_block) != collections.Counter(expected_block):
            raise InvalidInput(f"Managed block contains changed or extra rules in {label}; no content discarded")
        if previous_start <= last or any(active(s) for s in lines[last + 1:previous_start]):
            raise InvalidInput(f"Managed block is not immediately after precise country rules in {label}")
    nl = newline(text)
    if not lines[last].endswith(("\r", "\n")):
        lines[last] += nl
    block = [BEGIN, "# All precise country rules above are evaluated before these compatibility fallbacks."]
    block.extend(expected_block)
    block.append(END)
    lines[last + 1:last + 1] = [line + nl for line in block]
    return "".join(lines)


def load_baseline(path: Path, expected_countries: set[str]) -> dict[str, tuple[set[str], set[str]]]:
    if not path.is_file():
        raise InvalidInput(f"Missing country baseline registry: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("countries"), list):
        raise InvalidInput("Unsupported country baseline registry schema")
    baselines: dict[str, tuple[set[str], set[str]]] = {}
    for item in data["countries"]:
        cc = item["cc"]
        if cc in baselines:
            raise InvalidInput(f"Duplicate country in baseline registry: {cc}")
        aliases = item["aliases"]
        derived = (
            [f"DOMAIN,{x}" for x in aliases["exact"]]
            + [f"DOMAIN-SUFFIX,{x}" for x in aliases["suffix"]]
        )
        fixed_rules = {key(rule) for rule in item["fixed_ip_rules"]}
        normalized = {key(rule) for rule in derived} | fixed_rules
        if not normalized:
            raise InvalidInput(f"Empty baseline rule set for {cc}")
        baselines[cc] = (normalized, fixed_rules)
    if set(baselines) != expected_countries:
        raise InvalidInput("Baseline registry countries differ from maintenance contract")
    return baselines


def plan(repo: Path, contract: dict, baseline_path: Path | None = None) -> tuple[dict[Path, bytes], dict]:
    root = repo.resolve()
    output: dict[Path, bytes] = {}
    before: dict[Path, bytes | None] = {}

    def read(relative: Path, optional: bool = False) -> str:
        path = root / relative
        # Avoid writing through an unexpected output symlink outside this checkout.
        if not path.resolve().is_relative_to(root):
            raise InvalidInput(f"Path leaves repository: {relative}")
        if not path.exists():
            if not optional:
                raise InvalidInput(f"Missing input: {relative}")
            before[relative] = None
            return ""
        before[relative] = path.read_bytes()
        return before[relative].decode("utf-8")

    country_data = {c["cc"]: c for c in contract["countries"]}
    if baseline_path is None:
        baseline_path = root / "maintenance/country_registry.json"
        if not baseline_path.resolve().is_relative_to(root):
            raise InvalidInput("Baseline registry leaves repository")
    baselines = load_baseline(baseline_path, set(country_data))

    actual_paths = sorted((root / BASE / "rulesets").glob("WiFiCalling-*.list"))
    actual_countries = {m.group(1) for p in actual_paths if (m := re.fullmatch(r"WiFiCalling-([A-Z]{2})\.list", p.name))}
    unknown = actual_countries - country_data.keys()
    if unknown:
        raise InvalidInput(f"New countries need an explicit registry/policy review; files are unchanged: {sorted(unknown)}")
    all_formal = []
    all_known_geo = {key(s) for c in country_data.values() for s in c["geoip_fallback"]}
    country_counts = {}
    legacy_counts = {}
    added_counts = {}
    moved_counts = {}
    baseline_retained = {}
    for cc, item in country_data.items():
        relative = BASE / "rulesets" / f"WiFiCalling-{cc}.list"
        old = read(relative)
        if not rules(old):
            raise InvalidInput(f"Empty country input ruleset for {cc}; no files written")
        allowed = {key(s) for s in item["geoip_fallback"]}
        retained, moved = [], []
        for line in old.splitlines(keepends=True):
            value = active(line)
            if value and key(value) in allowed:
                moved.append(value)
            elif value and re.search(r"(?:^|[,(])\s*GEOIP\s*,", value):
                raise InvalidInput(f"Unknown GeoIP rule in {cc}; review it before publishing. No files written.")
            else:
                retained.append(line)
        new = "".join(retained)
        have = {key(s) for s in rules(new)}
        missing = [s for s in item["required_rules"] if key(s) not in have]
        if missing:
            new = append_lines(new, [ADD_COMMENT] + missing)
            have = {key(s) for s in rules(new)}
        baseline_set, fixed_rules = baselines[cc]
        missing_fixed = fixed_rules - have
        if missing_fixed:
            raise InvalidInput(f"Missing fixed IP rules for {cc}: {sorted(missing_fixed)}. No files written.")
        kept = len(baseline_set & have)
        if kept * 100 < len(baseline_set) * MIN_BASELINE_RETENTION_PCT:
            raise InvalidInput(
                f"Baseline retention below {MIN_BASELINE_RETENTION_PCT}% for {cc} "
                f"({kept}/{len(baseline_set)}); review and update country_registry.json first. No files written."
            )
        baseline_retained[cc] = kept
        output[relative] = new.encode("utf-8")
        country_counts[cc] = len(rules(new))
        added_counts[cc] = len(missing)
        moved_counts[cc] = len(moved)
        all_formal.extend(rules(new))

        legacy_path = BASE / "legacy_geoip" / f"WiFiCalling-{cc}-GeoIP.list"
        legacy = read(legacy_path, optional=True)
        if any(key(s) not in allowed for s in rules(legacy)):
            raise InvalidInput(f"Unknown existing legacy rule in {cc}; no files written")
        existing = {key(s) for s in rules(legacy)}
        legacy_missing = []
        for value in moved + item["geoip_fallback"]:
            if key(value) not in existing:
                legacy_missing.append(value)
                existing.add(key(value))
        if not legacy:
            legacy = f"# {cc} compatibility fallback; include only after ALL precise country rules.\n"
        legacy = append_lines(legacy, legacy_missing)
        output[legacy_path] = legacy.encode("utf-8")
        legacy_counts[cc] = len(rules(legacy))

    all_path = BASE / "rulesets/WiFiCalling-All.list"
    old_all = read(all_path)
    # Refuse to erase rules that exist only in the aggregate and cannot be assigned.
    old_formal = collections.Counter(key(s) for s in rules(old_all) if key(s) not in all_known_geo)
    planned_formal = collections.Counter(key(s) for s in all_formal)
    if old_formal - planned_formal:
        raise InvalidInput("Aggregate contains extra rules absent from country files; assign them before processing. No files written.")
    aggregate = "# Aggregate of formal country rules; excludes all legacy GeoIP fallbacks.\n"
    aggregate += "# Do not load together with the separate country rulesets.\n"
    for cc in country_data:
        aggregate += f"\n# {cc}\n"
        aggregate += "\n".join(rules(output[BASE / 'rulesets' / f'WiFiCalling-{cc}.list'].decode('utf-8'))) + "\n"
    output[all_path] = aggregate.encode("utf-8")
    summary_path = BASE / "SUMMARY.txt"
    output[summary_path] = summary(read(summary_path), country_counts, SUMMARY_NOTE).encode("utf-8")
    legacy_summary = BASE / "legacy_geoip/SUMMARY.txt"
    output[legacy_summary] = summary(read(legacy_summary, optional=True), legacy_counts, "# Compatibility fallback counts; excluded from the formal SUMMARY.").encode("utf-8")

    templates = []
    for name in ("main.conf", "merge_snippet.conf"):
        relative = BASE / name
        if (root / relative).exists():
            output[relative] = patch_template(read(relative), set(country_data), str(relative)).encode("utf-8")
            templates.append(str(relative))
    changed = {path: body for path, body in output.items() if before[path] != body}
    report = {
        "countries": len(country_data), "formal_rules": sum(country_counts.values()),
        "legacy_geoip_rules": sum(legacy_counts.values()), "country_counts": country_counts,
        "baseline_retained": baseline_retained,
        "rules_added": added_counts, "geoip_rules_moved": moved_counts,
        "public_templates_checked": templates,
        "changed_files": [str(p) for p in sorted(changed)],
        "result_sha256": {str(p): hashlib.sha256(body).hexdigest() for p, body in sorted(output.items())},
    }
    return changed, report


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=".wifi-postprocess-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.chmod(temporary, old_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path(__file__).with_name("registry.json"))
    parser.add_argument("--baseline", type=Path,
                        help="Approved baseline (default: --repo/maintenance/country_registry.json)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        changes, report = plan(args.repo, load_contract(args.registry), args.baseline)
        report["mode"] = "apply" if args.apply else "check" if args.check else "dry-run"
        if args.apply:
            for relative, body in changes.items():
                atomic_write(args.repo.resolve() / relative, body)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if args.check and changes else 0
    except (InvalidInput, OSError, ValueError, KeyError) as error:
        print(f"WiFi post-processing stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
