#!/usr/bin/env python3
"""Offline regression checks. Pass a local PUBLIC repository checkout as --fixture.

Only temporary copies are mutated; the supplied checkout is never changed.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
BASE = Path("outputs/surge_wifi_calling_country_carriers/v5")
CONTRACT = json.loads((HERE / "registry.json").read_text())
FIXTURE = None


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def read_rules(path):
    return [s.strip() for s in path.read_text().splitlines()
            if s.strip() and not s.lstrip().startswith(("#", ";", "//"))]


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wifi-regression-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        shutil.copytree(FIXTURE, self.repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    def run_tool(self, *flags):
        return subprocess.run([sys.executable, str(HERE / "postprocess_wifi.py"),
                               "--repo", str(self.repo), *flags],
                              capture_output=True, text=True)

    def apply(self):
        result = self.run_tool("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_stopped_without_writes(self):
        before = snapshot(self.repo)
        result = self.run_tool("--apply")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(snapshot(self.repo), before)

    def test_dry_run_preserves_every_byte(self):
        before = snapshot(self.repo)
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.repo), before)

    def test_apply_preserves_rules_and_unrelated_outputs(self):
        before = snapshot(self.repo)
        original = {c["cc"]: read_rules(self.repo / BASE / "rulesets" / f'WiFiCalling-{c["cc"]}.list')
                    for c in CONTRACT["countries"]}
        report = self.apply()
        formal = []
        for item in CONTRACT["countries"]:
            cc = item["cc"]
            current = read_rules(self.repo / BASE / "rulesets" / f"WiFiCalling-{cc}.list")
            legacy = read_rules(self.repo / BASE / "legacy_geoip" / f"WiFiCalling-{cc}-GeoIP.list")
            self.assertFalse(collections.Counter(original[cc]) - collections.Counter(current + legacy))
            self.assertTrue(set(item["required_rules"]) <= set(current))
            self.assertFalse(any("GEOIP," in s for s in current))
            self.assertEqual(set(legacy), set(item["geoip_fallback"]))
            formal += current
        self.assertEqual(collections.Counter(formal), collections.Counter(read_rules(self.repo / BASE / "rulesets/WiFiCalling-All.list")))
        self.assertEqual(report["formal_rules"], len(formal))
        self.assertEqual(report["legacy_geoip_rules"], 38)
        after = snapshot(self.repo)
        for path, digest in before.items():
            if not path.startswith(str(BASE) + "/"):
                self.assertEqual(after[path], digest, path)
        summary = (self.repo / BASE / "SUMMARY.txt").read_text()
        self.assertEqual(int(re.search(r"^TOTAL: rules=(\d+)$", summary, re.M)[1]), len(formal))

    def test_second_apply_and_check_are_byte_identical(self):
        self.apply()
        before = snapshot(self.repo)
        again = self.apply()
        self.assertEqual(again["changed_files"], [])
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.run_tool("--check").returncode, 0)

    def test_templates_only_receive_one_late_block(self):
        previous = {name: (self.repo / BASE / name).read_bytes()
                    for name in ("main.conf", "merge_snippet.conf")}
        self.apply()
        pattern = re.compile(rb"# BEGIN managed WiFi GeoIP fallbacks\r?\n.*?# END managed WiFi GeoIP fallbacks\r?\n", re.S)
        for name, original in previous.items():
            updated = (self.repo / BASE / name).read_bytes()
            # Accept fixtures already maintained by stripping the old block too.
            self.assertEqual(pattern.sub(b"", updated), pattern.sub(b"", original))
            self.assertEqual(updated.count(b"# BEGIN managed WiFi GeoIP fallbacks"), 1)
            lines = updated.decode().splitlines()
            precise = [i for i,s in enumerate(lines) if re.match(r"RULE-SET,.*rulesets/WiFiCalling-[A-Z]{2}\.list,", s)]
            legacy = [i for i,s in enumerate(lines) if s.startswith("RULE-SET,") and "legacy_geoip/" in s]
            self.assertEqual(len(legacy), 19)
            self.assertLess(max(precise), min(legacy))
            finals = [i for i,s in enumerate(lines) if s.startswith("FINAL,")]
            if finals:
                self.assertLess(max(legacy), min(finals))

    def test_generator_refresh_and_future_rules_survive(self):
        self.apply()
        # Simulate the original producer replacing its generated files tomorrow.
        for source in (FIXTURE / BASE).rglob("*"):
            if source.is_file():
                target = self.repo / BASE / source.relative_to(FIXTURE / BASE)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        future = ["DOMAIN,new-carrier.example", "IP-CIDR,203.0.113.0/24,no-resolve", "DOMAIN-SUFFIX,new-ims.example"]
        for relative in ("rulesets/WiFiCalling-GB.list", "rulesets/WiFiCalling-All.list"):
            path = self.repo / BASE / relative
            path.write_text(path.read_text() + "\n# Future upstream rules\n" + "\n".join(future) + "\n")
        self.apply()
        for relative in ("rulesets/WiFiCalling-GB.list", "rulesets/WiFiCalling-All.list"):
            self.assertTrue(set(future) <= set(read_rules(self.repo / BASE / relative)))
        self.assertEqual(self.run_tool("--check").returncode, 0)

    def test_new_unknown_country_stops_without_loss(self):
        path = self.repo / BASE / "rulesets/WiFiCalling-ZA.list"
        path.write_text("DOMAIN,new-country.example\n")
        self.assert_stopped_without_writes()

    def test_unknown_geoip_stops_without_loss(self):
        path = self.repo / BASE / "rulesets/WiFiCalling-GB.list"
        path.write_text(path.read_text() + "\nAND,((PROTOCOL,UDP),(DEST-PORT,123),(GEOIP,GB))\n")
        self.assert_stopped_without_writes()

    def test_aggregate_only_rule_stops_without_loss(self):
        path = self.repo / BASE / "rulesets/WiFiCalling-All.list"
        path.write_text(path.read_text() + "\nDOMAIN,aggregate-only.example\n")
        self.assert_stopped_without_writes()

    def test_early_final_stops_without_partial_update(self):
        path = self.repo / BASE / "main.conf"
        path.write_text(path.read_text().replace("[Rule]", "[Rule]\nFINAL,DIRECT", 1))
        self.assert_stopped_without_writes()

    def test_missing_country_stops_without_partial_update(self):
        (self.repo / BASE / "rulesets/WiFiCalling-GB.list").unlink()
        self.assert_stopped_without_writes()

    def test_double_slash_comments_are_preserved_not_counted(self):
        self.apply()
        original_count = len(read_rules(self.repo / BASE / "rulesets/WiFiCalling-GB.list"))
        path = self.repo / BASE / "rulesets/WiFiCalling-GB.list"
        path.write_text(path.read_text() + "// Comment mentioning GEOIP,GB is not a rule\n")
        report = self.apply()
        self.assertEqual(report["country_counts"]["GB"], original_count)
        self.assertIn("// Comment mentioning GEOIP,GB", path.read_text())

    def test_extra_rule_inside_managed_block_is_not_discarded(self):
        self.apply()
        path = self.repo / BASE / "main.conf"
        path.write_text(path.read_text().replace("# END managed WiFi GeoIP fallbacks", "DOMAIN,keep-this.example,DIRECT\n# END managed WiFi GeoIP fallbacks"))
        self.assert_stopped_without_writes()

    def test_managed_block_in_wrong_section_is_not_discarded(self):
        self.apply()
        path = self.repo / BASE / "main.conf"
        text = path.read_text()
        pattern = re.compile(r"# BEGIN managed WiFi GeoIP fallbacks\n.*?# END managed WiFi GeoIP fallbacks\n", re.S)
        block = pattern.search(text)[0]
        text = pattern.sub("", text).replace("[Rule]", block + "[Rule]", 1)
        path.write_text(text)
        self.assert_stopped_without_writes()

    def test_meaningful_regex_spaces_do_not_hide_aggregate_loss(self):
        country = self.repo / BASE / "rulesets/WiFiCalling-GB.list"
        aggregate = self.repo / BASE / "rulesets/WiFiCalling-All.list"
        country.write_text(country.read_text() + "\nURL-REGEX,^https://example.com/a[b]$\n")
        aggregate.write_text(aggregate.read_text() + "\nURL-REGEX,^https://example.com/a[ b]$\n")
        self.assert_stopped_without_writes()

    def test_empty_aggregate_is_rebuilt_from_intact_country_tables(self):
        self.apply()
        aggregate = self.repo / BASE / "rulesets/WiFiCalling-All.list"
        before = snapshot(self.repo)
        relative = aggregate.relative_to(self.repo).as_posix()
        for content in ("", "# Upstream did not fill in the aggregate\n"):
            with self.subTest(content=content):
                aggregate.write_text(content)
                self.assertEqual(self.run_tool("--check").returncode, 1)
                report = self.apply()
                self.assertEqual(report["changed_files"], [relative])
                self.assertEqual(snapshot(self.repo), before)
                self.assertEqual(self.run_tool("--check").returncode, 0)

    def test_missing_repo_baseline_requires_an_explicit_baseline(self):
        baseline = self.repo / "maintenance/country_registry.json"
        explicit = Path(self.temp.name) / "approved-country-registry.json"
        shutil.move(baseline, explicit)
        before = snapshot(self.repo)
        stopped = self.run_tool("--apply")
        self.assertEqual(stopped.returncode, 2, stopped.stdout + stopped.stderr)
        self.assertIn("Missing country baseline registry", stopped.stderr)
        self.assertEqual(snapshot(self.repo), before)
        accepted = self.run_tool("--apply", "--baseline", str(explicit))
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(self.run_tool("--check", "--baseline", str(explicit)).returncode, 0)

    def test_single_country_and_all_emptied_together_stops_without_loss(self):
        gb_path = self.repo / BASE / "rulesets/WiFiCalling-GB.list"
        gb_rules = set(read_rules(gb_path))
        gb_path.write_text("# Upstream glitch emptied GB\n")
        all_path = self.repo / BASE / "rulesets/WiFiCalling-All.list"
        remaining = [r for r in read_rules(all_path) if r not in gb_rules]
        all_path.write_text("\n".join(remaining) + "\n")
        self.assert_stopped_without_writes()

    def test_all_countries_and_aggregate_emptied_together_stops_without_loss(self):
        for item in CONTRACT["countries"]:
            (self.repo / BASE / "rulesets" / f'WiFiCalling-{item["cc"]}.list').write_text("# Empty\n")
        (self.repo / BASE / "rulesets/WiFiCalling-All.list").write_text("# Empty All\n")
        self.assert_stopped_without_writes()

    def test_fixed_networks_missing_from_country_and_aggregate_stop_without_writes(self):
        baseline = json.loads((HERE.parent / "country_registry.json").read_text())
        us = next(country for country in baseline["countries"] if country["cc"] == "US")
        fixed_rules = set(us["fixed_ip_rules"])
        self.assertEqual(len(fixed_rules), 2)
        for name in ("WiFiCalling-US.list", "WiFiCalling-All.list"):
            path = self.repo / BASE / "rulesets" / name
            current = read_rules(path)
            self.assertTrue(fixed_rules <= set(current))
            path.write_text("\n".join(rule for rule in current if rule not in fixed_rules) + "\n")
        before = snapshot(self.repo)
        result = self.run_tool("--apply")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Missing fixed IP rules for US", result.stderr)
        self.assertEqual(snapshot(self.repo), before)

    def test_mass_loss_padded_with_equal_count_filler_stops_without_loss(self):
        gb_item = next(c for c in CONTRACT["countries"] if c["cc"] == "GB")
        gb_path = self.repo / BASE / "rulesets/WiFiCalling-GB.list"
        original_gb = read_rules(gb_path)
        kept = list(gb_item["required_rules"])
        filler_count = len(original_gb) - len(kept)
        fillers = [f"DOMAIN,filler-{i}.example" for i in range(filler_count)]
        replacement_gb = kept + fillers
        self.assertEqual(len(replacement_gb), len(original_gb))
        gb_path.write_text("\n".join(replacement_gb) + "\n")
        all_rules = []
        for item in CONTRACT["countries"]:
            cc = item["cc"]
            all_rules.extend(read_rules(self.repo / BASE / "rulesets" / f"WiFiCalling-{cc}.list"))
        (self.repo / BASE / "rulesets/WiFiCalling-All.list").write_text("\n".join(all_rules) + "\n")
        self.assert_stopped_without_writes()

    def test_small_legitimate_update_passes_and_explicit_baseline_update_works(self):
        gb_item = next(c for c in CONTRACT["countries"] if c["cc"] == "GB")
        required_set = set(gb_item["required_rules"])
        gb_path = self.repo / BASE / "rulesets/WiFiCalling-GB.list"
        original_gb = read_rules(gb_path)
        removable = [r for r in original_gb if r not in required_set]
        # Remove 5 rules (~3.6% of 140, retaining > 90%): allowed without baseline edit.
        small_update_gb = [r for r in original_gb if r not in set(removable[:5])]
        gb_path.write_text("\n".join(small_update_gb) + "\n")
        all_rules = []
        for item in CONTRACT["countries"]:
            all_rules.extend(read_rules(self.repo / BASE / "rulesets" / f'WiFiCalling-{item["cc"]}.list'))
        (self.repo / BASE / "rulesets/WiFiCalling-All.list").write_text("\n".join(all_rules) + "\n")
        report = self.apply()
        self.assertEqual(report["country_counts"]["GB"], len(small_update_gb))
        self.assertEqual(self.run_tool("--check").returncode, 0)

        # Remove 25 rules (>10% of 140, retaining < 90%): blocked until baseline registry is updated.
        large_drop_set = set(removable[:25])
        large_update_gb = [r for r in original_gb if r not in large_drop_set]
        gb_path.write_text("\n".join(large_update_gb) + "\n")
        all_rules = []
        for item in CONTRACT["countries"]:
            all_rules.extend(read_rules(self.repo / BASE / "rulesets" / f'WiFiCalling-{item["cc"]}.list'))
        (self.repo / BASE / "rulesets/WiFiCalling-All.list").write_text("\n".join(all_rules) + "\n")
        self.assert_stopped_without_writes()

        baseline_path = self.repo / "maintenance/country_registry.json"
        registry_data = json.loads(baseline_path.read_text())
        for entry in registry_data["countries"]:
            if entry["cc"] == "GB":
                entry["aliases"]["exact"] = [d for d in entry["aliases"]["exact"] if f"DOMAIN,{d}" not in large_drop_set]
                entry["aliases"]["suffix"] = [s for s in entry["aliases"]["suffix"] if f"DOMAIN-SUFFIX,{s}" not in large_drop_set]
        baseline_path.write_text(json.dumps(registry_data, ensure_ascii=False, indent=2) + "\n")
        updated_report = self.apply()
        self.assertEqual(updated_report["country_counts"]["GB"], len(large_update_gb))
        self.assertEqual(self.run_tool("--check").returncode, 0)

    def test_v5_scope_does_not_create_unversioned_wifi_files(self):
        wifi_root = self.repo / "outputs/surge_wifi_calling_country_carriers"
        legacy_before = {
            str(p.relative_to(wifi_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in wifi_root.rglob("*")
            if p.is_file() and p.relative_to(wifi_root).parts[0] != "v5" and p.name != ".DS_Store"
        }
        self.apply()
        legacy_after = {
            str(p.relative_to(wifi_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in wifi_root.rglob("*")
            if p.is_file() and p.relative_to(wifi_root).parts[0] != "v5" and p.name != ".DS_Store"
        }
        self.assertEqual(legacy_after, legacy_before)

        for entry in list(wifi_root.iterdir()):
            if entry.name in ("v5", ".DS_Store"):
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        self.apply()
        entries = {p.name for p in wifi_root.iterdir() if p.name != ".DS_Store"}
        self.assertEqual(entries, {"v5"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    args = parser.parse_args()
    FIXTURE = args.fixture.resolve()
    if not (FIXTURE / BASE).is_dir():
        parser.error("--fixture must be a local public rules repository checkout")
    unittest.main(argv=[sys.argv[0], "-v"])
