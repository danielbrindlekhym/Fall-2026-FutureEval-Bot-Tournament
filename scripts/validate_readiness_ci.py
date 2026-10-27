"""Fail-closed validation for the proposed full offline candidate CI."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


EXPECTED_TESTS = 231
# Intended release path is scripts/validate_readiness_ci.py in the repository.
ROOT = Path(__file__).resolve().parents[1]
JUNIT = ROOT / "work" / "offline-tests.xml"
REPORT = ROOT / "outputs" / "offline-verification.json"


def fail(message: str) -> "NoReturn":
    raise SystemExit(message)


def main() -> None:
    if not JUNIT.is_file():
        fail("Required JUnit record is absent; full verification is unproven.")
    if not REPORT.is_file():
        fail("Required offline-verification.json is absent; full verification is unproven.")

    try:
        xml_root = ET.parse(JUNIT).getroot()
        report = json.loads(REPORT.read_text(encoding="utf-8"))
    except (ET.ParseError, OSError, json.JSONDecodeError) as exc:
        fail(f"Verification record is unreadable: {type(exc).__name__}")

    cases = list(xml_root.iter("testcase"))
    if xml_root.tag == "testsuite":
        count_source = [node for node in xml_root.iter("testsuite") if not list(node.iter("testsuite"))[1:]]
        if not count_source:
            count_source = [xml_root]
    elif xml_root.tag == "testsuites":
        suites = list(xml_root.iter("testsuite"))
        count_source = [node for node in suites if not any(child is not node for child in node.iter("testsuite"))]
    else:
        fail(f"Unsupported JUnit root element: {xml_root.tag}")
    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in count_source:
        for key in totals:
            value = suite.attrib.get(key)
            if value is None:
                fail(f"JUnit suite is missing its {key} counter.")
            try:
                number = int(value)
            except (TypeError, ValueError):
                fail(f"JUnit suite has a malformed {key} counter.")
            if number < 0:
                fail(f"JUnit suite has a negative {key} counter.")
            totals[key] += number
    if not count_source:
        fail("JUnit has no testsuite nodes with counters.")
    if len(cases) != EXPECTED_TESTS or totals["tests"] != EXPECTED_TESTS:
        fail(f"Expected exactly {EXPECTED_TESTS} collected JUnit cases; observed {len(cases)} cases and {totals['tests']} declared tests.")
    case_markers = {
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }
    if any(totals[key] != case_markers[key] for key in case_markers):
        fail(f"JUnit suite counters disagree with case records: {totals} versus {case_markers}.")
    if any(totals[key] != 0 for key in ("failures", "errors", "skipped")):
        fail(f"JUnit has failures, errors, or skipped tests: {totals}.")
    for case in cases:
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            fail("At least one JUnit test case did not pass.")

    checks = report.get("checks")
    if report.get("passed") is not True or report.get("test_count") != EXPECTED_TESTS:
        fail("offline-verification.json does not attest a passing 231-test run.")
    if not isinstance(checks, list) or len(checks) != EXPECTED_TESTS:
        fail("offline-verification.json does not contain exactly 231 check records.")
    if any(not isinstance(check, dict) or check.get("passed") is not True for check in checks):
        fail("offline-verification.json contains a failed, skipped, or malformed check.")

    try:
        sys.path.insert(0, str(ROOT))
        from futureeval_bot.core import Config, source_hashes

        if report.get("source_hashes") != source_hashes():
            fail("Reported source hashes do not match this checkout.")
        if report.get("config_fingerprint") != Config.load(ROOT / "config.json").fingerprint:
            fail("Reported config fingerprint does not match this checkout.")
    except (ImportError, OSError, ValueError, KeyError) as exc:
        fail(f"Could not validate report fingerprints: {type(exc).__name__}")

    required_identity = ("GITHUB_STEP_SUMMARY", "EXPECTED_HEAD_SHA", "EXPECTED_BASE_SHA", "GITHUB_RUN_ID")
    if any(not os.environ.get(key) for key in required_identity):
        fail("Run identity or GitHub job summary path is absent.")
    junit_sha256 = hashlib.sha256(JUNIT.read_bytes()).hexdigest()
    report_sha256 = hashlib.sha256(REPORT.read_bytes()).hexdigest()
    summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
    with summary.open("a", encoding="utf-8") as stream:
        stream.write(
        "## Readiness candidate full offline verification\n\n"
        f"- Workflow run ID: {os.environ['GITHUB_RUN_ID']}\n"
        f"- Candidate head: {os.environ['EXPECTED_HEAD_SHA']}\n"
        f"- Base commit: {os.environ['EXPECTED_BASE_SHA']}\n"
        f"- JUnit cases: {len(cases)}/{EXPECTED_TESTS}; failures/errors/skips: 0/0/0\n"
        f"- JSON checks: {len(checks)}/{EXPECTED_TESTS}; all passed\n"
        f"- Source hashes: `{json.dumps(report['source_hashes'], sort_keys=True)}`\n"
        f"- Config fingerprint: `{report['config_fingerprint']}`\n"
        f"- JUnit SHA-256: `{junit_sha256}`\n"
        f"- JSON report SHA-256: `{report_sha256}`\n"
        "- Test network: new outbound traffic blocked; localhost permitted\n"
        "- Credentials/secrets, model/feed calls, forecast submissions, and bot-state writes: not provided or performed\n",
        )


if __name__ == "__main__":
    main()
