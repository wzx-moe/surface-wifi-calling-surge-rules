"""Contract tests for the strict adapter and failed-update preservation. No network."""
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("update_rules", HERE / "update_rules.py")
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="surge-ai-test-")
        self.root = Path(self.temp.name)
        self.src = self.root / "maintenance/ai_source"
        shutil.copytree(HERE, self.src, ignore=shutil.ignore_patterns("__pycache__"))
        self.fixtures = self.src / "fixtures"
        u.run(self.root, self.fixtures)

    def tearDown(self):
        self.temp.cleanup()

    def outputs(self):
        output = self.root / "outputs/surge_ai"
        return {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()}

    def reject_preserving_outputs(self, expected=""):
        before = self.outputs()
        with self.assertRaisesRegex(u.ValidationError, expected):
            u.run(self.root, self.fixtures)
        self.assertEqual(before, self.outputs())

    def test_current_sources_cover_official_and_legacy_requirements(self):
        result = u.run(self.root, verify_current=True)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(set(result["output_rule_counts"]), set(u.NAMES))
        for name in u.NAMES:
            body = (self.root / "outputs/surge_ai" / (name + ".list")).read_text()
            self.assertNotIn("\nDOMAIN-REGEX,", body)
            self.assertNotIn("PROCESS-NAME", body)

    def test_identical_sources_are_byte_stable(self):
        before = self.outputs()
        result = u.run(self.root, self.fixtures)
        self.assertEqual(result["changed_files"], [])
        self.assertEqual(before, self.outputs())

    def test_new_valid_upstream_domain_is_published(self):
        path = self.fixtures / "OpenAI.list"
        path.write_text(path.read_text() + "\nDOMAIN,new-service.openai.com\n")
        u.run(self.root, self.fixtures)
        body = (self.root / "outputs/surge_ai/OpenAI.list").read_text()
        self.assertIn("\nDOMAIN,new-service.openai.com\n", body)
        u.run(self.root, verify_current=True)

    def test_invalid_last_service_never_publishes_valid_first_service(self):
        path = self.fixtures / "OpenAI.list"
        path.write_text(path.read_text() + "\nDOMAIN,new-service.openai.com\n")
        path = self.fixtures / "Antigravity.list"
        path.write_text(path.read_text() + "\nPROCESS-NAME,example\n")
        self.reject_preserving_outputs("Unsupported")

    def test_unknown_regex_requires_review(self):
        path = self.fixtures / "OpenAI.list"
        path.write_text(path.read_text() + "\nDOMAIN-REGEX,^.*example.*$\n")
        self.reject_preserving_outputs("Unknown upstream regex")

    def test_policy_injection_is_rejected(self):
        path = self.fixtures / "Claude.list"
        path.write_text(path.read_text() + "\nDOMAIN,evil.example,DIRECT\n")
        self.reject_preserving_outputs("exactly a type and value")

    def test_html_error_document_is_rejected(self):
        (self.fixtures / "Claude.list").write_text("<html>Rate limited</html>")
        self.reject_preserving_outputs("exactly a type and value")

    def test_source_count_collapse_is_rejected(self):
        (self.fixtures / "Antigravity.list").write_text(
            "DOMAIN,antigravity.google\nDOMAIN,cloudcode-pa.googleapis.com\nDOMAIN,gemini.google.com\n")
        self.reject_preserving_outputs("count")

    def test_gradual_core_domain_loss_is_rejected(self):
        path = self.fixtures / "Claude.list"
        path.write_text(path.read_text().replace("DOMAIN-SUFFIX,claude.com\n", ""))
        self.reject_preserving_outputs("lost a service anchor")

    def test_equal_count_replacement_losing_domain_coverage_is_rejected(self):
        path = self.fixtures / "Antigravity.list"
        lines = u.active(path.read_text())
        anchors = {"DOMAIN-SUFFIX,antigravity.google", "DOMAIN,cloudcode-pa.googleapis.com",
                   "DOMAIN,gemini.google.com"}
        replaced = [rule if rule in anchors else "DOMAIN-SUFFIX,replacement%d.example" % i
                    for i, rule in enumerate(lines)]
        path.write_text("\n".join(replaced) + "\n")
        self.reject_preserving_outputs("previously covered domain scope")

    def test_required_official_dependency_loss_is_rejected(self):
        path = self.src / "supplements.json"
        data = json.loads(path.read_text())
        data["rules"]["OpenAI"].remove("DOMAIN,challenges.cloudflare.com")
        path.write_text(json.dumps(data))
        self.reject_preserving_outputs("Required/legacy coverage missing")

    def test_new_shared_cloud_parent_is_rejected(self):
        path = self.fixtures / "Antigravity.list"
        path.write_text(path.read_text() + "\nDOMAIN-SUFFIX,googleapis.com\n")
        self.reject_preserving_outputs("broad shared-cloud")

    def test_known_regex_translation_covers_named_hosts_without_catching_other_azure(self):
        regex = next(iter(u.REGEX_TRANSLATIONS))
        rule = u.REGEX_TRANSLATIONS[regex]
        for name in (
            "chatgpt-async-webps-prod-eastus-1.webpubsub.azure.com",
            "chatgpt-async-webps-prod-us-east2-123456789.webpubsub.azure.com",
            "chatgpt-async-webps-prod-one.two-12.webpubsub.azure.com",
        ):
            self.assertIsNotNone(re.fullmatch(regex, name))
            self.assertTrue(u.matches(rule, name))
        for name in (
            "other-app.webpubsub.azure.com",
            "chatgpt-async-webps-prod-eastus-x.webpubsub.azure.com",
            "chatgpt-async-webps-prod-eastus-1.webpubsub.azure.com.attacker.example",
        ):
            self.assertFalse(u.matches(rule, name))
        # Documented widening after the initial digit; never claim exact equivalence.
        widened = "chatgpt-async-webps-prod-eastus-1x.webpubsub.azure.com"
        self.assertIsNone(re.fullmatch(regex, widened))
        self.assertTrue(u.matches(rule, widened))

    def test_suffix_coverage_does_not_accept_one_sample_domain(self):
        self.assertFalse(u.covers(["DOMAIN,probe.openai.com"], "DOMAIN-SUFFIX,openai.com"))
        self.assertTrue(u.covers(["DOMAIN-SUFFIX,openai.com"], "DOMAIN-SUFFIX,auth.openai.com"))

    def test_modified_generated_file_is_detected(self):
        path = self.root / "outputs/surge_ai/Claude.list"
        path.write_text(path.read_text() + "\nDOMAIN,unreviewed.example\n")
        with self.assertRaisesRegex(u.ValidationError, "Current output/lock mismatch"):
            u.run(self.root, verify_current=True)

    def test_filesystem_failure_rolls_back_earlier_replacement(self):
        before = self.outputs()
        real_replace = u.os.replace
        count = 0
        def fail_second(source, target):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated one-time filesystem failure")
            return real_replace(source, target)
        with mock.patch.object(u.os, "replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                u.atomic_publish(self.root / "outputs/surge_ai",
                                 {"OpenAI.list": b"first", "Claude.list": b"second"})
        self.assertEqual(before, self.outputs())

    def test_unreviewed_source_url_is_rejected_without_network(self):
        path = self.src / "sources.json"
        data = json.loads(path.read_text())
        data["sources"]["Claude"]["url"] = "https://unreviewed.example/rules"
        path.write_text(json.dumps(data))
        self.reject_preserving_outputs("Unreviewed source URL")

    def test_unexpected_network_redirect_is_rejected(self):
        response = io.BytesIO(b"DOMAIN,example.com")
        response.status = 200
        response.url = "https://unreviewed.example/rules"
        with mock.patch.object(u.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(u.ValidationError, "status/redirect"):
                u.fetch(u.BASE + "openai.list", 262144)

    def test_known_exact_google_login_rules_do_not_block_unrelated_updates(self):
        before = self.outputs()
        shared_rules = ("DOMAIN,accounts.google.com", "DOMAIN,oauth2.googleapis.com")
        antigravity = self.fixtures / "Antigravity.list"
        antigravity.write_text(antigravity.read_text() + "\n" + "\n".join(shared_rules) + "\n")
        openai = self.fixtures / "OpenAI.list"
        new_rule = "DOMAIN,new-service.openai.com"
        openai.write_text(openai.read_text() + "\n" + new_rule + "\n")

        result = u.run(self.root, self.fixtures)
        after = self.outputs()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(u.active(after["Antigravity.list"].decode()),
                         u.active(before["Antigravity.list"].decode()))
        self.assertTrue(set(shared_rules) <= set(u.active(after["upstream/Antigravity.list"].decode())))
        self.assertIn(new_rule, u.active(after["OpenAI.list"].decode()))
        self.assertEqual(u.run(self.root, verify_current=True)["status"], "PASS")

    def test_shared_google_login_endpoints_are_excluded_and_cannot_be_recaptured(self):
        tables = {name: u.active((self.root / "outputs/surge_ai" / f"{name}.list").read_text())
                  for name in u.RULE_ORDER}
        for host in ("accounts.google.com", "oauth2.googleapis.com"):
            self.assertIsNone(u.first_matching_service(tables, host))

        antigravity_fixture = self.fixtures / "Antigravity.list"
        base_antigravity = antigravity_fixture.read_text()
        for injected in ("DOMAIN-SUFFIX,accounts.google.com", "DOMAIN-SUFFIX,oauth2.googleapis.com"):
            antigravity_fixture.write_text(base_antigravity + f"\n{injected}\n")
            self.reject_preserving_outputs("Forbidden shared login host captured")

        with mock.patch.dict(u.REGEX_TRANSLATIONS, {
            r"^accounts\.google\..*$": "DOMAIN-WILDCARD,accounts.google.*",
            r"^.*oauth2\.googleapis\.com$": "DOMAIN-WILDCARD,*oauth2.googleapis.com",
        }):
            antigravity_fixture.write_text(base_antigravity + "\nDOMAIN-REGEX,^accounts\\.google\\..*$\n")
            self.reject_preserving_outputs("Forbidden shared login host captured")
            antigravity_fixture.write_text(base_antigravity + "\nDOMAIN-REGEX,^.*oauth2\\.googleapis\\.com$\n")
            self.reject_preserving_outputs("Forbidden shared login host captured")
        antigravity_fixture.write_text(base_antigravity)

    def test_ordered_precedence_claude_before_openai_before_antigravity(self):
        tables = {name: u.active((self.root / "outputs/surge_ai" / f"{name}.list").read_text())
                  for name in u.RULE_ORDER}
        self.assertEqual(u.first_matching_service(tables, "anthropic.auth0.com"), "Claude")
        self.assertEqual(u.first_matching_service(tables, "auth0.com"), "OpenAI")
        self.assertEqual(u.first_matching_service(tables, "sub.auth0.com"), "OpenAI")
        self.assertEqual(u.first_matching_service(tables, "antigravity.googleapis.com"), "Antigravity")
        self.assertEqual(u.first_matching_service(tables, "antigravity.google.com"), "Antigravity")

        claude_fixture = self.fixtures / "Claude.list"
        base_claude = claude_fixture.read_text()
        # Historical regression: earlier Claude list stealing antigravity.googleapis.com.
        claude_fixture.write_text(base_claude + "\nDOMAIN,antigravity.googleapis.com\n")
        self.reject_preserving_outputs("Ordered routing conflict.*Antigravity.*antigravity\\.googleapis\\.com.*Claude")

        # Claude stealing OpenAI suffix or keyword probe must fail.
        claude_fixture.write_text(base_claude + "\nDOMAIN,sub.openai.com\n")
        self.reject_preserving_outputs("Ordered routing conflict.*OpenAI.*DOMAIN-SUFFIX,openai\\.com.*Claude")
        claude_fixture.write_text(base_claude + "\nDOMAIN,openai-keyword-probe.example\n")
        self.reject_preserving_outputs("Ordered routing conflict.*OpenAI.*DOMAIN-KEYWORD,openai.*Claude")
        claude_fixture.write_text(base_claude)

        # OpenAI stealing Antigravity required host must fail.
        openai_fixture = self.fixtures / "OpenAI.list"
        base_openai = openai_fixture.read_text()
        openai_fixture.write_text(base_openai + "\nDOMAIN,cloudcode-pa.googleapis.com\n")
        self.reject_preserving_outputs("Ordered routing conflict.*Antigravity.*cloudcode-pa\\.googleapis\\.com.*OpenAI")
        openai_fixture.write_text(base_openai)


if __name__ == "__main__":
    unittest.main(verbosity=2)
