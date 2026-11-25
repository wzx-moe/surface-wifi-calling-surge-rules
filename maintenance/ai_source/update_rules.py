#!/usr/bin/env python3
"""Strict MetaCubeX -> Surge AI format adapter. Standard library, no node subscription access."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fnmatch
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.request

NAMES = ("OpenAI", "Claude", "Antigravity")
RULE_ORDER = ("Claude", "OpenAI", "Antigravity")
FORBIDDEN_SHARED_HOSTS = ("accounts.google.com", "oauth2.googleapis.com")
EXCLUDED_UPSTREAM_RULES = frozenset("DOMAIN," + host for host in FORBIDDEN_SHARED_HOSTS)
BASE = "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/classical/"
EXPECTED = dict(zip(NAMES, ("openai.list", "anthropic.list", "google-gemini.list")))
REGEX_TRANSLATIONS = {
    r"^chatgpt-async-webps-prod-\S+-\d+\.webpubsub\.azure\.com$":
        "DOMAIN-WILDCARD,chatgpt-async-webps-prod-?*-[0-9]*.webpubsub.azure.com",
}
REGEX_NOTE = (
    "The reviewed Azure hostname glob preserves all valid-hostname matches of the known regex. "
    "Its numeric tail permits extra characters after the initial digit; this is a documented "
    "bounded widening under the chatgpt-async-webps-prod prefix, not exact regex equivalence."
)
BLOCKED_UPSTREAM_SUFFIXES = {
    "com", "net", "org", "io", "ai", "google.com", "googleapis.com", "gstatic.com",
    "cloudflare.com", "azure.com", "amazonaws.com", "github.com", "githubusercontent.com",
    "auth0.com", "sentry.io", "stripe.com",
}
DOMAIN = re.compile(r"(?=.{1,253}$)[a-z0-9_-]+(?:\.[a-z0-9_-]+)+\Z")


class ValidationError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ValidationError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def active(text):
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", ";", "//"))]


def validate_rule(rule, *, upstream=False):
    require(rule.count(",") == 1, "Rule must have exactly a type and value, with no policy/flags")
    kind, value = rule.split(",")
    if kind in {"DOMAIN", "DOMAIN-SUFFIX"}:
        require(bool(DOMAIN.fullmatch(value)), "Invalid domain syntax")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise ValidationError("IP literal is not accepted as a domain rule")
        require(not upstream or value not in BLOCKED_UPSTREAM_SUFFIXES,
                "New broad shared-cloud suffix requires manual review")
    elif kind == "DOMAIN-KEYWORD":
        require(not upstream and value == "openai", "Only the reviewed legacy openai keyword is allowed")
    elif kind == "DOMAIN-WILDCARD":
        require(rule in REGEX_TRANSLATIONS.values(), "Only reviewed wildcard translations are allowed")
    else:
        raise ValidationError("Unsupported Surge output rule type: " + kind)
    return rule


def convert(text):
    rules, translations = [], []
    for line in active(text):
        if line.startswith("DOMAIN-REGEX,"):
            regex = line.split(",", 1)[1]
            require(regex in REGEX_TRANSLATIONS, "Unknown upstream regex requires explicit review")
            rule = REGEX_TRANSLATIONS[regex]
            translations.append({"upstream": line, "surge": rule, "note": REGEX_NOTE})
            validate_rule(rule)
        else:
            rule = line.lower() if "," not in line else line.split(",", 1)[0] + "," + line.split(",", 1)[1].lower()
            validate_rule(rule, upstream=True)
        rules.append(rule)
    return sorted(set(rules)), translations


def matches(rule, hostname):
    kind, value = rule.split(",", 1)
    hostname = hostname.lower().rstrip(".")
    if kind == "DOMAIN":
        return hostname == value
    if kind == "DOMAIN-SUFFIX":
        return hostname == value or hostname.endswith("." + value)
    if kind == "DOMAIN-KEYWORD":
        return value in hostname
    if kind == "DOMAIN-WILDCARD":
        return fnmatch.fnmatchcase(hostname, value)
    return False


def covers(rules, requirement):
    if requirement in rules:
        return True
    kind, value = requirement.split(",", 1)
    if kind == "DOMAIN-SUFFIX":
        # One probe cannot establish coverage of an entire suffix namespace.
        return any((r.startswith("DOMAIN-SUFFIX,") and
                    (value == r.split(",", 1)[1] or value.endswith("." + r.split(",", 1)[1])))
                   or (r.startswith("DOMAIN-KEYWORD,") and r.split(",", 1)[1] in value)
                   for r in rules)
    if kind == "DOMAIN-KEYWORD":
        return requirement in rules
    return any(matches(r, value) for r in rules)


def probe_hosts(requirement):
    kind, value = requirement.split(",", 1)
    if kind == "DOMAIN":
        return (value,)
    if kind == "DOMAIN-SUFFIX":
        return (value, "sub." + value)
    if kind == "DOMAIN-KEYWORD":
        return (f"{value}-keyword-probe.example",)
    raise ValidationError("Unsupported requirement rule type: " + kind)


def first_matching_service(tables, hostname):
    for service in RULE_ORDER:
        if any(matches(rule, hostname) for rule in tables[service]):
            return service
    return None


def load_configuration(root):
    source_dir = root / "maintenance/ai_source"
    documents = {name: (source_dir / name).read_bytes()
                 for name in ("sources.json", "supplements.json", "requirements.json")}
    config, supplement, required = [json.loads(documents[name]) for name in
                                     ("sources.json", "supplements.json", "requirements.json")]
    require(set(config["sources"]) == set(NAMES), "Expected three reviewed services")
    for name in NAMES:
        require(config["sources"][name]["url"] == BASE + EXPECTED[name], "Unreviewed source URL")
        require(config["sources"][name]["file"] == name + ".list", "Unreviewed output path")
    return config, supplement, required, {k: sha(v) for k, v in documents.items()}


def fetch(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "Surge-AI-Rule-Adapter/1",
                                                  "Accept": "text/plain"})
    with urllib.request.urlopen(request, timeout=30) as response:
        require(response.status == 200 and response.url == url, "Unexpected upstream status/redirect")
        raw = response.read(limit + 1)
    require(0 < len(raw) <= limit, "Upstream response is empty or too large")
    raw.decode("utf-8", errors="strict")
    return raw


def assemble(root, payloads, previous=None):
    config, supplement, required, config_hashes = load_configuration(root)
    result, details, assembled_rules = {}, {}, {}
    previous = previous or {}
    for name in NAMES:
        raw = payloads[name]
        require(0 < len(raw) <= config["maximum_source_bytes"], "Invalid source size: " + name)
        upstream, translations = convert(raw.decode("utf-8", errors="strict"))
        count = len(upstream)
        spec = config["sources"][name]
        require(spec["minimum_upstream_rules"] <= count <= config["maximum_upstream_rules"],
                "Upstream count outside reviewed bounds: " + name)
        prior = previous.get("services", {}).get(name, {}).get("upstream_rule_count")
        if prior:
            require(prior * config["minimum_count_ratio_to_last_good"] <= count
                    <= prior * config["maximum_count_ratio_to_last_good"],
                    "Upstream coverage size changed too sharply: " + name)
        prior_rules = previous.get("services", {}).get(name, {}).get("normalized_upstream_rules", [])
        if prior_rules:
            retained = sum(covers(upstream, rule) for rule in prior_rules)
            require(retained >= len(prior_rules) * config["minimum_prior_rule_coverage"],
                    "Upstream lost too much previously covered domain scope: " + name)
        for host in spec["required_upstream_hosts"]:
            require(any(matches(r, host) for r in upstream), "Upstream lost a service anchor: " + name)
        extras = [validate_rule(r) for r in supplement["rules"][name]]
        combined = sorted((set(upstream) - EXCLUDED_UPSTREAM_RULES).union(extras))
        for host in FORBIDDEN_SHARED_HOSTS:
            require(not any(matches(r, host) for r in combined),
                    f"Forbidden shared login host captured: {name} {host}")
        for rule in required["required"][name]:
            require(covers(combined, rule), "Required/legacy coverage missing: " + name + " " + rule)
        assembled_rules[name] = combined
        body = "\n".join([
            "# " + name + " | Surge RULE-SET | domain routing only",
            "# Upstream: " + spec["url"],
            "# Source SHA256: " + sha(raw),
            "# Adapter and exact supplements: maintenance/ai_source/",
            "# Updates are published only after syntax, coverage and size checks pass.",
            "# Native DOMAIN-WILDCARD translation is documented in source-lock.json."
            if translations else "# No unsupported upstream regex or policy field is passed through.",
            "# Rule count: " + str(len(combined)),
            *combined, "",
        ]).encode()
        result[name + ".list"] = body
        result["upstream/" + name + ".list"] = raw
        details[name] = {
            "url": spec["url"], "upstream_sha256": sha(raw), "upstream_rule_count": count,
            "normalized_upstream_rules": upstream,
            "output_sha256": sha(body), "output_rule_count": len(combined),
            "required_cases": len(required["required"][name]), "regex_translations": translations,
        }
    for service in RULE_ORDER:
        for rule in required["required"][service]:
            for host in probe_hosts(rule):
                winner = first_matching_service(assembled_rules, host)
                require(winner == service,
                        f"Ordered routing conflict ({' -> '.join(RULE_ORDER)}): "
                        f"{service} requirement {rule} probe {host} matched {winner}")
    lock = {
        "schema_version": 1, "accepted_at_utc": datetime.now(timezone.utc).isoformat(),
        "upstream_repository": "https://github.com/MetaCubeX/meta-rules-dat",
        "upstream_branch": "meta", "commit_sha": None,
        "provenance_note": "Raw snapshots plus content SHA256 are the reproducible lock; no unobserved git commit is claimed.",
        "configuration_sha256": config_hashes, "adapter_sha256": sha(Path(__file__).read_bytes()),
        "services": details, "native_surge_validation": False,
    }
    # Keep a stable last-content-change timestamp if nothing changed.
    comparison = lambda d: {k: v for k, v in d.items() if k != "accepted_at_utc"}
    if comparison(previous) == comparison(lock):
        lock["accepted_at_utc"] = previous["accepted_at_utc"]
    result["source-lock.json"] = (json.dumps(lock, ensure_ascii=False, indent=2) + "\n").encode()
    return result, lock


def atomic_publish(destination, files):
    """Validate first; replace individual files atomically. Git publishes the set as one commit."""
    destination.mkdir(parents=True, exist_ok=True)
    prior = {name: (destination / name).read_bytes() if (destination / name).exists() else None
             for name in files}
    changed = [name for name, data in files.items() if prior[name] != data]
    with tempfile.TemporaryDirectory(prefix=".ai-stage-", dir=destination.parent) as temp:
        stage = Path(temp)
        for name in changed:
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[name])
        installed = []
        try:
            for name in changed:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage / name, target)
                installed.append(name)
        except OSError:
            # Restore earlier replacements if a later filesystem operation failed.
            for name in reversed(installed):
                target = destination / name
                if prior[name] is None:
                    target.unlink()
                else:
                    restore = stage / name
                    restore.parent.mkdir(parents=True, exist_ok=True)
                    restore.write_bytes(prior[name])
                    os.replace(restore, target)
            raise
    return changed


def run(root, source_dir=None, check_only=False, verify_current=False):
    root = Path(root).resolve()
    config, _, _, _ = load_configuration(root)
    destination = root / "outputs/surge_ai"
    lock_path = destination / "source-lock.json"
    previous = json.loads(lock_path.read_text()) if lock_path.exists() else None
    if verify_current:
        require(previous is not None, "No current output lock")
        source_dir = destination / "upstream"
        check_only = True
    if source_dir is not None:
        payloads = {name: (Path(source_dir) / (name + ".list")).read_bytes() for name in NAMES}
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {name: pool.submit(fetch, config["sources"][name]["url"],
                                         config["maximum_source_bytes"]) for name in NAMES}
            payloads = {name: futures[name].result() for name in NAMES}
    files, lock = assemble(root, payloads, previous)
    if verify_current:
        for name, data in files.items():
            require((destination / name).read_bytes() == data, "Current output/lock mismatch: " + name)
    changed = [] if check_only else atomic_publish(destination, files)
    return {"status": "PASS", "check_only": check_only, "changed_files": changed,
            "output_rule_counts": {name: lock["services"][name]["output_rule_count"] for name in NAMES},
            "native_surge_validation": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--source-dir", type=Path, help="Use local public snapshots; no network")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--verify-current", action="store_true", help="Check generated files and locked snapshots offline")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.root, args.source_dir, args.check_only, args.verify_current),
                         ensure_ascii=False))
    except (ValidationError, OSError, ValueError) as error:
        raise SystemExit("AI update failed; no Git publication was performed. Details: " + str(error))
