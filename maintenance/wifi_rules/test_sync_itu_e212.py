#!/usr/bin/env python3
"""Offline unit and integration tests for ITU-T E.212B Wi-Fi Calling V5 sync."""
from __future__ import annotations

import collections
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from xml.sax.saxutils import escape
import zipfile

HERE = Path(__file__).resolve().parent
REPO_FIXTURE = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import sync_itu_e212  # noqa: E402

BASE = Path("outputs/surge_wifi_calling_country_carriers/v5")


def build_synthetic_docx(sections: list[tuple[str, list[tuple[str, str]]]]) -> bytes:
    """Build a minimal valid WordprocessingML .docx matching ITU-T E.212B structure."""
    rows_xml = [
        "<w:tr><w:trPr><w:tblHeader/></w:trPr>"
        "<w:tc><w:p><w:r><w:t>Country or Geographical Area</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Network</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>MCC MNC</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
    ]
    for area, entries in sections:
        rows_xml.append(
            "<w:tr>"
            f"<w:tc><w:tcPr><w:vMerge w:val=\"restart\"/></w:tcPr><w:p><w:r><w:t>{escape(area)}</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p/></w:tc><w:tc><w:p/></w:tc>"
            "</w:tr>"
        )
        for operator, code in entries:
            rows_xml.append(
                "<w:tr>"
                "<w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>"
                f"<w:tc><w:p><w:r><w:t>{escape(operator)}</w:t></w:r></w:p></w:tc>"
                f"<w:tc><w:p><w:r><w:t>{escape(code)}</w:t></w:r></w:p></w:tc>"
                "</w:tr>"
            )

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:tbl>{''.join(rows_xml)}</w:tbl></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", document_xml.encode("utf-8"))
    return buf.getvalue()


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def read_rules(path: Path) -> list[str]:
    return [
        s.strip()
        for s in path.read_text(encoding="utf-8").splitlines()
        if s.strip() and not s.lstrip().startswith(("#", ";", "//"))
    ]


class SyncItuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="itu-sync-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        shutil.copytree(REPO_FIXTURE, self.repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    def _build_full_synthetic_docx(self, extra_rows: list[tuple[str, list[tuple[str, str]]]] | None = None) -> bytes:
        sections: list[tuple[str, list[tuple[str, str]]]] = [
            ("United Kingdom*", [("Synthetic GB NewCo", "234 99"), ("O2 UK", "234 02")]),
            ("United States", [("Synthetic US TestNet", "310 999"), ("AT&T", "310 410")]),
            ("International Mobile*", [("Global Satellite", "901 99")]),
        ]
        if extra_rows:
            sections.extend(extra_rows)
        # Pad with enough valid non-target areas/records to easily verify threshold logic in small tests.
        return build_synthetic_docx(sections)

    def test_parse_synthetic_docx_handles_header_vmerge_and_footnotes(self) -> None:
        docx = build_synthetic_docx([
            ("United Kingdom*", [("Carrier   Alpha", "234 99"), ("Bad Row", "invalid")]),
            ("France", [("Carrier Beta", "208 01")]),
        ])
        records, areas = sync_itu_e212.parse_e212b_docx(docx, min_records=2, min_areas=2)
        self.assertEqual(areas, {"United Kingdom", "France"})
        self.assertEqual(
            records,
            [
                ("United Kingdom", "Carrier Alpha", "234", "099"),
                ("France", "Carrier Beta", "208", "001"),
            ],
        )

    def test_monotonic_sync_and_postprocess_pipeline_and_idempotency(self) -> None:
        docx = self._build_full_synthetic_docx()
        tw_before = (self.repo / BASE / "rulesets/WiFiCalling-TW.list").read_bytes()
        gb_before_rules = read_rules(self.repo / BASE / "rulesets/WiFiCalling-GB.list")
        us_before_rules = read_rules(self.repo / BASE / "rulesets/WiFiCalling-US.list")

        changed, report = sync_itu_e212.sync_repository(
            self.repo,
            docx_bytes=docx,
            edition="T-SP-E.212B-2099",
            source_url="https://www.itu.int/dms_pub/itu-t/opb/sp/T-SP-E.212B-2099-MSW-E.docx",
            min_records=4,
            min_areas=3,
        )
        for rel, body in changed.items():
            sync_itu_e212.atomic_write(self.repo / rel, body)

        # Unapproved MCC 901 must not enter any file; TW has no ITU changes and stays byte-identical.
        self.assertEqual((self.repo / BASE / "rulesets/WiFiCalling-TW.list").read_bytes(), tw_before)
        gb_after = (self.repo / BASE / "rulesets/WiFiCalling-GB.list").read_text(encoding="utf-8")
        us_after = (self.repo / BASE / "rulesets/WiFiCalling-US.list").read_text(encoding="utf-8")
        self.assertIn("DOMAIN-SUFFIX,mnc099.mcc234.3gppnetwork.org", gb_after)
        self.assertIn("DOMAIN-SUFFIX,mnc099.mcc234.pub.3gppnetwork.org", gb_after)
        self.assertIn("DOMAIN-SUFFIX,mnc999.mcc310.3gppnetwork.org", us_after)
        self.assertIn("DOMAIN-SUFFIX,mnc999.mcc310.pub.3gppnetwork.org", us_after)
        self.assertNotIn("mcc901", gb_after + us_after)
        # Base rules preserved monotonically (no removals, no duplicates of existing 234-002 / 310-410).
        gb_after_rules = read_rules(self.repo / BASE / "rulesets/WiFiCalling-GB.list")
        us_after_rules = read_rules(self.repo / BASE / "rulesets/WiFiCalling-US.list")
        self.assertEqual(len(gb_after_rules), len(gb_before_rules) + 2)
        self.assertEqual(len(us_after_rules), len(us_before_rules) + 2)
        self.assertEqual(len(gb_after_rules), len(set(gb_after_rules)))
        self.assertEqual(report["synced_rules_total"], 4)

        # Run postprocess_wifi --apply and --check.
        apply_proc = subprocess.run(
            [sys.executable, str(HERE / "postprocess_wifi.py"), "--repo", str(self.repo), "--apply"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(apply_proc.returncode, 0, apply_proc.stderr)
        pp_report = json.loads(apply_proc.stdout)
        self.assertEqual(pp_report["formal_rules"], 2497 + 4)
        self.assertEqual(pp_report["legacy_geoip_rules"], 38)

        check_proc = subprocess.run(
            [sys.executable, str(HERE / "postprocess_wifi.py"), "--repo", str(self.repo), "--check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(check_proc.returncode, 0, check_proc.stderr)

        # Second sync run with identical input must be a complete no-op (no fake timestamp commits).
        before_second = snapshot(self.repo)
        changed2, _ = sync_itu_e212.sync_repository(
            self.repo,
            docx_bytes=docx,
            edition="T-SP-E.212B-2099",
            source_url="https://www.itu.int/dms_pub/itu-t/opb/sp/T-SP-E.212B-2099-MSW-E.docx",
            min_records=4,
            min_areas=3,
        )
        self.assertEqual(changed2, {})
        self.assertEqual(snapshot(self.repo), before_second)

    def test_monotonic_preservation_when_future_edition_drops_synced_mnc(self) -> None:
        docx_v1 = self._build_full_synthetic_docx()
        changed1, _ = sync_itu_e212.sync_repository(
            self.repo,
            docx_bytes=docx_v1,
            edition="T-SP-E.212B-2099",
            source_url="https://www.itu.int/dms_pub/itu-t/opb/sp/T-SP-E.212B-2099-MSW-E.docx",
            min_records=4,
            min_areas=3,
        )
        for rel, body in changed1.items():
            sync_itu_e212.atomic_write(self.repo / rel, body)
        subprocess.run(
            [sys.executable, str(HERE / "postprocess_wifi.py"), "--repo", str(self.repo), "--apply"],
            check=True,
            capture_output=True,
        )

        # Future edition drops 234-99 and adds 234-98. 234-99 MUST remain preserved!
        docx_v2 = build_synthetic_docx([
            ("United Kingdom", [("Synthetic GB Second", "234 98")]),
            ("United States", [("Synthetic US TestNet", "310 999")]),
            ("France", [("Carrier Beta", "208 01")]),
        ])
        changed2, _ = sync_itu_e212.sync_repository(
            self.repo,
            docx_bytes=docx_v2,
            edition="T-SP-E.212B-2100",
            source_url="https://www.itu.int/dms_pub/itu-t/opb/sp/T-SP-E.212B-2100-MSW-E.docx",
            min_records=3,
            min_areas=3,
        )
        for rel, body in changed2.items():
            sync_itu_e212.atomic_write(self.repo / rel, body)

        gb_rules = read_rules(self.repo / BASE / "rulesets/WiFiCalling-GB.list")
        self.assertIn("DOMAIN-SUFFIX,mnc099.mcc234.3gppnetwork.org", gb_rules)
        self.assertIn("DOMAIN-SUFFIX,mnc098.mcc234.3gppnetwork.org", gb_rules)

        apply2 = subprocess.run(
            [sys.executable, str(HERE / "postprocess_wifi.py"), "--repo", str(self.repo), "--apply"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(apply2.returncode, 0, apply2.stderr)
        all_rules = read_rules(self.repo / BASE / "rulesets/WiFiCalling-All.list")
        self.assertEqual(collections.Counter(all_rules)["DOMAIN-SUFFIX,mnc099.mcc234.3gppnetwork.org"], 1)

    def test_fail_closed_on_corrupt_or_truncated_input_and_disallowed_urls(self) -> None:
        before = snapshot(self.repo)
        bad_docx = self.repo / "bad.docx"
        bad_docx.write_bytes(b"not a zip archive")
        proc = subprocess.run(
            [sys.executable, str(HERE / "sync_itu_e212.py"), "--repo", str(self.repo), "--docx", str(bad_docx), "--apply"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Invalid ITU .docx", proc.stderr)
        bad_docx.unlink()
        self.assertEqual(snapshot(self.repo), before)

        tiny_docx = self.repo / "tiny.docx"
        tiny_docx.write_bytes(self._build_full_synthetic_docx())
        proc2 = subprocess.run(
            [sys.executable, str(HERE / "sync_itu_e212.py"), "--repo", str(self.repo), "--docx", str(tiny_docx), "--apply"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc2.returncode, 2)
        self.assertIn("below minimum sanity threshold", proc2.stderr)
        tiny_docx.unlink()
        self.assertEqual(snapshot(self.repo), before)

        for bad_url in ("http://www.itu.int/pub/T-SP-E.212B", "https://evil.example.com/e212b.docx"):
            with self.assertRaises(sync_itu_e212.SyncError):
                sync_itu_e212.validate_itu_url(bad_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
