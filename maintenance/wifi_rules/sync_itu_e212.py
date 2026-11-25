#!/usr/bin/env python3
"""Sync official ITU-T E.212B MCC/MNC additions into Wi-Fi Calling V5 country lists.

Standard library only. Strictly monotonic/additive over existing base rules and
previously synced entries so curated FQDNs, fixed CIDRs, and non-ITU regions (e.g. TW)
are never removed. Writes nothing in default dry-run or --check modes.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

BASE = Path("outputs/surge_wifi_calling_country_carriers/v5")
LOCKFILE_REL = BASE / "itu_e212_lock.json"
INDEX_URL = "https://www.itu.int/pub/T-SP-E.212B"
ALLOWED_HOSTS = frozenset({"www.itu.int"})
USER_AGENT = "surface-wifi-calling-surge-rules/1.0"
REQUEST_TIMEOUT_S = 30
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_DOCX_BYTES = 16 * 1024 * 1024
MAX_XML_BYTES = 32 * 1024 * 1024
MIN_ITU_RECORDS = 1500
MIN_ITU_AREAS = 150

BEGIN_MARKER = "# BEGIN ITU-T E.212 synced additions"
END_MARKER = "# END ITU-T E.212 synced additions"
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

RULE_RE = re.compile(r"^DOMAIN-SUFFIX,mnc(\d{3})\.mcc(\d{3})\.(?:pub\.)?3gppnetwork\.org$")
COMMENT_MNC_RE = re.compile(r"^#\s*MCC-MNC:\s*(\d{3})-(\d{3})(?:\s*\|\s*(.*))?$")


class SyncError(ValueError):
    pass


def validate_itu_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        raise SyncError(f"Blocked non-ITU or non-HTTPS URL: {url}")
    return url


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        validate_itu_url(target)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_itu_bytes(url: str, *, max_bytes: int) -> bytes:
    validate_itu_url(url)
    opener = urllib.request.build_opener(_AllowlistedRedirectHandler)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    with opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
        validate_itu_url(response.geturl())
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise SyncError(f"Response exceeded {max_bytes} bytes for {url}")
            chunks.append(chunk)
    return b"".join(chunks)


def discover_latest_docx_url(fetcher=fetch_itu_bytes) -> tuple[str, str]:
    index_html = fetcher(INDEX_URL, max_bytes=MAX_HTML_BYTES).decode("utf-8", errors="replace")
    editions = re.findall(r"parent=(T-SP-E\.212B-(\d{4}))", index_html)
    if not editions:
        raise SyncError("Could not find any E.212B edition links on ITU index page")
    latest_edition, _ = max(editions, key=lambda item: (int(item[1]), item[0]))
    edition_url = f"https://www.itu.int/pub/{latest_edition}"
    edition_html = fetcher(edition_url, max_bytes=MAX_HTML_BYTES).decode("utf-8", errors="replace")
    matches = re.findall(r'href\s*=\s*["\']([^"\']*-E\.docx)["\']', edition_html, re.IGNORECASE)
    if not matches:
        raise SyncError(f"Could not find English .docx download link for {latest_edition}")
    preferred = next((m for m in matches if "E.212B" in m), matches[0])
    docx_url = urllib.parse.urljoin("https://www.itu.int", html.unescape(preferred))
    return latest_edition, validate_itu_url(docx_url)


def _cell_text(tc: ET.Element) -> str:
    chunks: list[str] = []
    for elem in tc.iter():
        tag = elem.tag
        if tag == f"{W_NS}t":
            if elem.text:
                chunks.append(elem.text)
        elif tag in (f"{W_NS}tab", f"{W_NS}br"):
            chunks.append(" ")
        elif tag == f"{W_NS}p" and chunks and not chunks[-1].endswith(" "):
            chunks.append(" ")
    return re.sub(r"\s+", " ", "".join(chunks)).strip()


def _read_bounded_zip_entry(docx_bytes: bytes, entry_name: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
            info = archive.getinfo(entry_name)
            if info.file_size > MAX_XML_BYTES:
                raise SyncError(f"ZIP entry {entry_name} exceeds {MAX_XML_BYTES} bytes")
            with archive.open(info, "r") as handle:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_XML_BYTES:
                        raise SyncError(f"Decompressed {entry_name} exceeds {MAX_XML_BYTES} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise SyncError(f"Invalid ITU .docx archive: {exc}") from exc


def parse_e212b_docx(
    docx_bytes: bytes,
    *,
    min_records: int = MIN_ITU_RECORDS,
    min_areas: int = MIN_ITU_AREAS,
) -> tuple[list[tuple[str, str, str, str]], set[str]]:
    """Parse (area, operator, mcc, mnc3) records from an ITU-T E.212B .docx."""
    xml_bytes = _read_bounded_zip_entry(docx_bytes, "word/document.xml")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise SyncError(f"Malformed word/document.xml: {exc}") from exc

    target_table = None
    for tbl in root.findall(f".//{W_NS}tbl"):
        if any(vm.get(f"{W_NS}val") == "restart" for vm in tbl.iter(f"{W_NS}vMerge")):
            target_table = tbl
            break
    if target_table is None:
        raise SyncError("Could not find MCC/MNC table with vMerge restart in ITU document")

    records: list[tuple[str, str, str, str]] = []
    areas: set[str] = set()
    current_area: str | None = None

    for tr in target_table.findall(f"{W_NS}tr"):
        tr_pr = tr.find(f"{W_NS}trPr")
        if tr_pr is not None and tr_pr.find(f"{W_NS}tblHeader") is not None:
            continue
        tcs = tr.findall(f"{W_NS}tc")
        if not tcs:
            continue

        first_vm = tcs[0].find(f"{W_NS}tcPr/{W_NS}vMerge")
        first_vm_val = first_vm.get(f"{W_NS}val") if first_vm is not None else None
        if first_vm is not None and first_vm_val == "restart":
            area_name = _cell_text(tcs[0]).rstrip("*").strip()
            if area_name:
                current_area = area_name
                areas.add(current_area)
            # Some table variants may place row data in subsequent cells on restart.
            remaining = tcs[1:]
        else:
            remaining = []
            for tc in tcs:
                vm = tc.find(f"{W_NS}tcPr/{W_NS}vMerge")
                if vm is not None and vm.get(f"{W_NS}val") != "restart":
                    continue
                remaining.append(tc)

        if not current_area or len(remaining) < 2:
            continue
        operator = _cell_text(remaining[0])
        code_text = _cell_text(remaining[1])
        match = re.fullmatch(r"(\d{3})\s+(\d{2,3})", code_text)
        if not operator or not match:
            continue
        mcc, mnc3 = match.group(1), match.group(2).zfill(3)
        records.append((current_area, operator, mcc, mnc3))

    if len(records) < min_records or len(areas) < min_areas:
        raise SyncError(
            f"ITU parse below minimum sanity threshold: records={len(records)} (min {min_records}), "
            f"areas={len(areas)} (min {min_areas})"
        )
    return records, areas


def load_approved_countries(baseline_path: Path) -> list[tuple[str, list[str]]]:
    if not baseline_path.is_file():
        raise SyncError(f"Missing country baseline registry: {baseline_path}")
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("countries"), list):
        raise SyncError("Unsupported country baseline registry schema")
    countries: list[tuple[str, list[str]]] = []
    seen_cc: set[str] = set()
    seen_mcc: dict[str, str] = {}
    for item in data["countries"]:
        cc = item["cc"]
        mccs = item.get("mcc")
        if not re.fullmatch(r"[A-Z]{2}", cc) or cc in seen_cc:
            raise SyncError(f"Invalid or duplicate country in registry: {cc}")
        if not isinstance(mccs, list) or not mccs or any(not re.fullmatch(r"\d{3}", m) for m in mccs):
            raise SyncError(f"Invalid MCC list for {cc}: {mccs}")
        for mcc in mccs:
            if mcc in seen_mcc:
                raise SyncError(f"MCC {mcc} assigned to both {seen_mcc[mcc]} and {cc}")
            seen_mcc[mcc] = cc
        seen_cc.add(cc)
        countries.append((cc, list(mccs)))
    return countries


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _split_managed_block(text: str, label: str) -> tuple[list[str], list[str], bool]:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == BEGIN_MARKER]
    ends = [i for i, line in enumerate(lines) if line.strip() == END_MARKER]
    if not starts and not ends:
        return lines, [], False
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise SyncError(f"Malformed ITU managed block markers in {label}")
    base_lines = lines[: starts[0]] + lines[ends[0] + 1 :]
    block_lines = lines[starts[0] + 1 : ends[0]]
    return base_lines, block_lines, True


def _active_rule(line: str) -> str:
    val = line.strip().lstrip("\ufeff")
    return "" if not val or val.startswith(("#", ";", "//")) else val


def _normalize_rule(rule: str) -> str:
    if "," in rule:
        return ",".join(part.strip() for part in rule.split(","))
    return rule.strip()


def _parse_previous_managed(block_lines: list[str]) -> dict[tuple[str, str], set[str]]:
    prev: dict[tuple[str, str], set[str]] = {}
    last_pair: tuple[str, str] | None = None
    for line in block_lines:
        stripped = line.strip()
        cm = COMMENT_MNC_RE.match(stripped)
        if cm:
            pair = (cm.group(1), cm.group(2))
            ops_text = (cm.group(3) or "").strip()
            ops = {o.strip() for o in ops_text.split(";") if o.strip()} if ops_text else set()
            prev.setdefault(pair, set()).update(ops)
            last_pair = pair
            continue
        rm = RULE_RE.match(_normalize_rule(stripped))
        if rm:
            pair = (rm.group(2), rm.group(1))
            prev.setdefault(pair, set())
            last_pair = pair
            continue
        if stripped and not stripped.startswith("#"):
            raise SyncError(f"Unexpected non-3GPP rule inside ITU managed block: {stripped}")
        _ = last_pair
    return prev


def sync_repository(
    repo: Path,
    *,
    baseline_path: Path | None = None,
    docx_bytes: bytes | None = None,
    edition: str | None = None,
    source_url: str | None = None,
    min_records: int = MIN_ITU_RECORDS,
    min_areas: int = MIN_ITU_AREAS,
    fetcher=fetch_itu_bytes,
) -> tuple[dict[Path, bytes], dict]:
    root = repo.resolve()
    if baseline_path is None:
        baseline_path = root / "maintenance/country_registry.json"
        if not baseline_path.resolve().is_relative_to(root):
            raise SyncError("Baseline registry leaves repository")

    countries = load_approved_countries(baseline_path)

    if docx_bytes is None:
        resolved_edition, resolved_url = discover_latest_docx_url(fetcher=fetcher)
        docx_bytes = fetcher(resolved_url, max_bytes=MAX_DOCX_BYTES)
        edition = edition or resolved_edition
        source_url = source_url or resolved_url
    else:
        edition = edition or "offline-docx"
        source_url = source_url or INDEX_URL

    docx_sha256 = hashlib.sha256(docx_bytes).hexdigest()
    records, areas = parse_e212b_docx(docx_bytes, min_records=min_records, min_areas=min_areas)

    itu_ops_by_pair: dict[tuple[str, str], set[str]] = {}
    for _, op, mcc, mnc3 in records:
        itu_ops_by_pair.setdefault((mcc, mnc3), set()).add(op)

    before: dict[Path, bytes | None] = {}
    planned: dict[Path, bytes] = {}
    synced_pairs_by_country: dict[str, int] = {}
    synced_rules_by_country: dict[str, int] = {}

    for cc, mccs in countries:
        rel = BASE / "rulesets" / f"WiFiCalling-{cc}.list"
        path = root / rel
        if not path.resolve().is_relative_to(root):
            raise SyncError(f"Path leaves repository: {rel}")
        if not path.is_file():
            raise SyncError(f"Missing country ruleset: {rel}")
        raw_bytes = path.read_bytes()
        before[rel] = raw_bytes
        raw_text = raw_bytes.decode("utf-8")
        nl = _newline(raw_text)

        base_lines, block_lines, had_block = _split_managed_block(raw_text, str(rel))
        base_rules = {_normalize_rule(r) for line in base_lines if (r := _active_rule(line))}
        prev_synced = _parse_previous_managed(block_lines)

        merged_ops: dict[tuple[str, str], set[str]] = {}
        for pair, ops in prev_synced.items():
            if pair[0] in mccs:
                merged_ops.setdefault(pair, set()).update(ops)
            else:
                # Preserve anything previously synced even if MCC list changed.
                merged_ops.setdefault(pair, set()).update(ops)

        for (mcc, mnc3), ops in itu_ops_by_pair.items():
            if mcc in mccs:
                merged_ops.setdefault((mcc, mnc3), set()).update(ops)

        entries_to_emit: list[tuple[tuple[str, str], list[str], list[str]]] = []
        added_rule_count = 0
        for mcc, mnc3 in sorted(merged_ops):
            candidates = [
                f"DOMAIN-SUFFIX,mnc{mnc3}.mcc{mcc}.3gppnetwork.org",
                f"DOMAIN-SUFFIX,mnc{mnc3}.mcc{mcc}.pub.3gppnetwork.org",
            ]
            missing_rules = [rule for rule in candidates if rule not in base_rules]
            if missing_rules:
                ops_sorted = sorted(merged_ops[(mcc, mnc3)])
                entries_to_emit.append(((mcc, mnc3), ops_sorted, missing_rules))
                added_rule_count += len(missing_rules)

        synced_pairs_by_country[cc] = len(entries_to_emit)
        synced_rules_by_country[cc] = added_rule_count

        if not entries_to_emit and not had_block:
            planned[rel] = raw_bytes
            continue

        while base_lines and not base_lines[-1].strip():
            base_lines.pop()

        if entries_to_emit:
            block = [
                BEGIN_MARKER,
                f"# Source: {edition} ({source_url})",
            ]
            for (mcc, mnc3), ops_sorted, missing_rules in entries_to_emit:
                label = "; ".join(ops_sorted) if ops_sorted else "ITU-T E.212"
                block.append(f"# MCC-MNC: {mcc}-{mnc3} | {label}")
                block.extend(missing_rules)
            block.append(END_MARKER)
            new_text = nl.join(base_lines) + (nl * 2 if base_lines else "") + nl.join(block) + nl
        else:
            new_text = (nl.join(base_lines) + nl) if base_lines else ""

        planned[rel] = new_text.encode("utf-8")

    lock_path = root / LOCKFILE_REL
    if not lock_path.resolve().is_relative_to(root):
        raise SyncError(f"Lockfile leaves repository: {LOCKFILE_REL}")
    before[LOCKFILE_REL] = lock_path.read_bytes() if lock_path.exists() else None
    lock_payload = {
        "schema_version": 1,
        "upstream": "ITU-T Recommendation E.212 Annex (E.212B)",
        "index_url": INDEX_URL,
        "edition": edition,
        "source_url": source_url,
        "docx_sha256": docx_sha256,
        "total_itu_records": len(records),
        "total_itu_areas": len(areas),
        "synced_mnc_pairs_by_country": synced_pairs_by_country,
        "synced_rules_by_country": synced_rules_by_country,
        "synced_rules_total": sum(synced_rules_by_country.values()),
    }
    planned[LOCKFILE_REL] = (json.dumps(lock_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    changed = {rel: body for rel, body in planned.items() if before[rel] != body}
    report = {
        "edition": edition,
        "source_url": source_url,
        "docx_sha256": docx_sha256,
        "total_itu_records": len(records),
        "total_itu_areas": len(areas),
        "synced_mnc_pairs_by_country": synced_pairs_by_country,
        "synced_rules_total": sum(synced_rules_by_country.values()),
        "changed_files": [str(p) for p in sorted(changed)],
    }
    return changed, report


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=".itu-sync-", dir=path.parent)
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
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Approved baseline (default: --repo/maintenance/country_registry.json)",
    )
    parser.add_argument("--docx", type=Path, help="Optional local E.212B .docx file for offline sync")
    parser.add_argument("--edition", help="Optional edition label when using --docx")
    parser.add_argument("--source-url", help="Optional source URL metadata when using --docx")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        docx_bytes = args.docx.read_bytes() if args.docx is not None else None
        changes, report = sync_repository(
            args.repo,
            baseline_path=args.baseline,
            docx_bytes=docx_bytes,
            edition=args.edition,
            source_url=args.source_url,
        )
        report["mode"] = "apply" if args.apply else "check" if args.check else "dry-run"
        if args.apply:
            root = args.repo.resolve()
            for rel, body in changes.items():
                atomic_write(root / rel, body)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if args.check and changes else 0
    except (SyncError, OSError, ValueError, KeyError) as error:
        print(f"ITU E.212 sync stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
