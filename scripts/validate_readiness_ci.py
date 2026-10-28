"""Fail-closed validation for the proposed full offline candidate CI."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


EXPECTED_TESTS = 257
EXPECTED_BASE_SHA = "1c9fc16ef8c4752472e712a9d3f3c1707e779da7"
EXPECTED_BASE_TREE = "7d10c90c5f2e96facf73a7a255de9118beb06b2a"
EXPECTED_BASE_PARENTS = ["517b2a89aa51c2b56b933e7520516a43a874a6be", "c65ad9ffefa6c8f632e0d6d9fb094abb9443c74e"]
EXPECTED_SOURCE_SHA = "b8215556c2c3efcb37486b0e633de2f06e2a573f"
EXPECTED_SOURCE_TREE = "58191fcb4292b12bb0f546ed7622a86366197398"
# Intended release path is scripts/validate_readiness_ci.py in the repository.
ROOT = Path(__file__).resolve().parents[1]
JUNIT = ROOT / "work" / "offline-tests.xml"
REPORT = ROOT / "outputs" / "offline-verification.json"


def fail(message: str) -> "NoReturn":
    raise SystemExit(message)


def parse_commit_identity(commit_object: bytes) -> tuple[str, list[str]]:
    headers, separator, _message = commit_object.partition(b"\n\n")
    if not separator:
        raise ValueError("Commit object has no header/message separator.")
    trees: list[str] = []
    parents: list[str] = []
    for line in headers.splitlines():
        if line.startswith(b"tree"):
            if not line.startswith(b"tree "):
                raise ValueError("Commit object has a malformed tree header.")
            trees.append(line[5:].decode("ascii"))
        elif line.startswith(b"parent"):
            if not line.startswith(b"parent "):
                raise ValueError("Commit object has a malformed parent header.")
            parents.append(line[7:].decode("ascii"))
    if len(trees) != 1 or not re.fullmatch(r"[0-9a-f]{40}", trees[0]):
        raise ValueError("Commit object must have exactly one valid tree header.")
    if any(not re.fullmatch(r"[0-9a-f]{40}", parent) for parent in parents):
        raise ValueError("Commit object has a malformed parent identity.")
    return trees[0], parents


def commit_matches(commit_sha: str, expected_tree: str | None, expected_parents: list[str]) -> bool:
    raw = subprocess.check_output(["git", "cat-file", "-p", commit_sha], cwd=ROOT)
    tree, parents = parse_commit_identity(raw)
    subprocess.check_call(["git", "cat-file", "-e", f"{tree}^{{tree}}"], cwd=ROOT, stdout=subprocess.DEVNULL)
    return (expected_tree is None or tree == expected_tree) and parents == expected_parents


def commit_chain_matches(head_sha: str) -> bool:
    return (
        commit_matches(head_sha, None, [EXPECTED_SOURCE_SHA])
        and commit_matches(EXPECTED_SOURCE_SHA, EXPECTED_SOURCE_TREE, [EXPECTED_BASE_SHA])
        and commit_matches(EXPECTED_BASE_SHA, EXPECTED_BASE_TREE, EXPECTED_BASE_PARENTS)
    )


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
        fail(f"offline-verification.json does not attest a passing {EXPECTED_TESTS}-test run.")
    if not isinstance(checks, list) or len(checks) != EXPECTED_TESTS:
        fail(f"offline-verification.json does not contain exactly {EXPECTED_TESTS} check records.")
    if any(not isinstance(check, dict) or check.get("passed") is not True for check in checks):
        fail("offline-verification.json contains a failed, skipped, or malformed check.")

    try:
        sys.path.insert(0, str(ROOT))
        from futureeval_bot.core import Config, source_hashes

        actual_source_hashes = source_hashes()
        if report.get("source_hashes") != actual_source_hashes:
            fail("Reported source hashes do not match this checkout.")
        if "futureeval_bot/operational_summary.py" not in actual_source_hashes:
            fail("Operational-summary module is missing from the verified source-hash set.")
        if report.get("config_fingerprint") != Config.load(ROOT / "config.json").fingerprint:
            fail("Reported config fingerprint does not match this checkout.")
    except (ImportError, OSError, ValueError, KeyError) as exc:
        fail(f"Could not validate report fingerprints: {type(exc).__name__}")

    required_identity = (
        "GITHUB_STEP_SUMMARY",
        "EXPECTED_HEAD_SHA",
        "EXPECTED_BASE_SHA",
        "EXPECTED_SOURCE_SHA",
        "GITHUB_RUN_ID",
    )
    if any(not os.environ.get(key) for key in required_identity):
        fail("Run identity or GitHub job summary path is absent.")
    if os.environ["EXPECTED_BASE_SHA"] != EXPECTED_BASE_SHA:
        fail("Pull-request base does not match the reviewed source base.")
    if os.environ["EXPECTED_SOURCE_SHA"] != EXPECTED_SOURCE_SHA:
        fail("Reviewed source commit identity does not match.")
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        fail(f"Could not validate checked-out commit identity: {type(exc).__name__}")
    if head != os.environ["EXPECTED_HEAD_SHA"]:
        fail("Checked-out candidate commit differs from the GitHub pull-request head.")
    try:
        if not commit_chain_matches(head):
            fail("Fetched candidate/source/base commit trees or complete parent lists differ from reviewed identities.")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError, ValueError) as exc:
        fail(f"Could not validate raw fetched commit headers: {type(exc).__name__}")
    junit_sha256 = hashlib.sha256(JUNIT.read_bytes()).hexdigest()
    report_sha256 = hashlib.sha256(REPORT.read_bytes()).hexdigest()
    summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
    with summary.open("a", encoding="utf-8") as stream:
        stream.write(
        "## Readiness candidate full offline verification\n\n"
        f"- Workflow run ID: {os.environ['GITHUB_RUN_ID']}\n"
        f"- Candidate head: {os.environ['EXPECTED_HEAD_SHA']}\n"
        f"- Base commit: {os.environ['EXPECTED_BASE_SHA']}\n"
        f"- Source-only commit: {os.environ['EXPECTED_SOURCE_SHA']}\n"
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
