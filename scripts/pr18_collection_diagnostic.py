"""PR18-only sanitized collection diagnostic, invoked as Python stdin from repo root."""

import errno
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import pathlib
import re
import runpy
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout

import pytest


ROOT = pathlib.Path.cwd().resolve()
DIAG_ROOT = pathlib.Path(os.environ["DIAGNOSTIC_ROOT"])
TAG = os.environ["DIAGNOSTIC_TAG"]
SAFE_EXCEPTION_CLASSES = {
    "AssertionError", "ConftestImportFailure", "FileNotFoundError", "ImportError",
    "KeyError", "ModuleNotFoundError", "OSError", "Other", "PermissionError",
    "RuntimeError", "TypeError", "ValueError",
}
SAFE_EXCEPTION_MODULES = {
    "_pytest.config", "builtins", "futureeval_bot", "importlib", "os", "pluggy",
    "pytest", "socket", "tempfile", "tests", "other",
}
SAFE_TOP_LEVEL_MODULES = {
    "_pytest", "builtins", "futureeval_bot", "importlib", "os", "pluggy",
    "pytest", "socket", "tempfile", "tests", "none", "other",
}


def atomic_json(path, value):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)


def valid_name(value, dotted=False):
    if not isinstance(value, str) or not value or len(value) > 128:
        return False
    return all(part.isidentifier() for part in (value.split(".") if dotted else [value]))


def safe_exception_class(value):
    return value if value in SAFE_EXCEPTION_CLASSES else "Other"


def safe_exception_module(value):
    return value if value in SAFE_EXCEPTION_MODULES else "other"


def safe_top_level_module(value):
    top_level = value.split(".", 1)[0] if isinstance(value, str) else "other"
    return top_level if top_level in SAFE_TOP_LEVEL_MODULES else "other"


def named_rule(lines, marker, required):
    matches = [line for line in lines if marker in line]
    return len(matches) == 1 and all(piece in matches[0] for piece in required)


def test_loopback_socket():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server.settimeout(1)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(server.getsockname())
                accepted, _ = server.accept()
                accepted.close()
        return True
    except OSError:
        return False


def test_new_outbound_socket_is_blocked():
    allowed_rejections = {
        errno.ECONNREFUSED,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.EACCES,
        errno.EPERM,
    }
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            return probe.connect_ex(("198.51.100.1", 443)) in allowed_rejections
    except OSError as exc:
        return exc.errno in allowed_rejections


def preflight():
    checks = {}
    tmp = pathlib.Path(os.environ["TMPDIR"]).resolve()
    checks["cwd_is_checkout_root"] = (
        (ROOT / "tests" / "conftest.py").is_file()
        and (ROOT / "futureeval_bot").is_dir()
    )
    checks["checkout_root_on_sys_path"] = "" in sys.path or any(
        pathlib.Path(entry or ".").resolve() == ROOT for entry in sys.path
    )
    checks["effective_temp_matches_env"] = pathlib.Path(tempfile.gettempdir()).resolve() == tmp
    try:
        fd, name = tempfile.mkstemp(prefix="pr18-preflight-", dir=str(tmp))
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"bounded-diagnostic")
            stream.flush()
            os.fsync(stream.fileno())
        checks["tempfile_roundtrip"] = pathlib.Path(name).read_bytes() == b"bounded-diagnostic"
        pathlib.Path(name).unlink()
    except OSError:
        checks["tempfile_roundtrip"] = False
    try:
        with pathlib.Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write("\n### PR18 sanitized collection preflight\n")
        checks["summary_file_writable"] = True
    except OSError:
        checks["summary_file_writable"] = False
    try:
        from _pytest.capture import CaptureManager

        manager = CaptureManager("fd")
        manager.start_global_capturing()
        manager.stop_global_capturing()
        checks["pytest_capture_initializes"] = True
    except BaseException:
        checks["pytest_capture_initializes"] = False
    checks["pytest_discoverable"] = importlib.util.find_spec("pytest") is not None
    checks["futureeval_bot_discoverable"] = importlib.util.find_spec("futureeval_bot") is not None
    try:
        runpy.run_path(str(ROOT / "tests" / "conftest.py"), run_name="__pr18_preflight_conftest__")
        checks["conftest_import"] = True
    except BaseException:
        checks["conftest_import"] = False

    try:
        v4 = subprocess.run(["sudo", "iptables-save", "-t", "filter"], capture_output=True, text=True)
        v6 = subprocess.run(["sudo", "ip6tables-save", "-t", "filter"], capture_output=True, text=True)
        v4_lines = v4.stdout.splitlines() if v4.returncode == 0 else []
        v6_lines = v6.stdout.splitlines() if v6.returncode == 0 else []
        checks["loopback_rule_v4"] = named_rule(v4_lines, TAG + "-v4-lo", ["-o lo", "-j ACCEPT"])
        checks["loopback_rule_v6"] = named_rule(v6_lines, TAG + "-v6-lo", ["-o lo", "-j ACCEPT"])
        checks["established_rule_v4"] = named_rule(
            v4_lines, TAG + "-v4-established", ["--ctstate ESTABLISHED,RELATED", "-j ACCEPT"]
        )
        checks["established_rule_v6"] = named_rule(
            v6_lines, TAG + "-v6-established", ["--ctstate ESTABLISHED,RELATED", "-j ACCEPT"]
        )
        checks["blocked_new_socket_rule_v4"] = named_rule(v4_lines, TAG + "-v4-reject", ["-j REJECT"])
        checks["blocked_new_socket_rule_v6"] = named_rule(v6_lines, TAG + "-v6-reject", ["-j REJECT"])
        checks["loopback_socket_allowed"] = test_loopback_socket()
        checks["blocked_new_socket_probe_v4"] = test_new_outbound_socket()
    except BaseException:
        for key in (
            "loopback_rule_v4", "loopback_rule_v6", "established_rule_v4", "established_rule_v6",
            "blocked_new_socket_rule_v4", "blocked_new_socket_rule_v6", "loopback_socket_allowed",
            "blocked_new_socket_probe_v4",
        ):
            checks[key] = False
    return checks


def collection_result():
    names = {
        "test_complete_feed_requires_terminal_page_and_successful_discovery",
        "test_every_invalid_continuation_fails_closed_after_one_page",
        "test_exact_operation_binding_and_unknown_fallback",
        "test_failed_diagnostic_persistence_does_not_mask_primary_error",
        "test_invalid_post_telemetry_failure_preserves_metadata_error_and_subtype",
        "test_metadata_and_state_write_failures_are_distinct",
        "test_page_30_terminal_and_multi_page_contiguous_feed_complete_at_existing_cap",
        "test_page_30_valid_continuation_fails_without_request_31",
        "test_read_only_operational_summary_emits_only_bound_diagnostic_schema",
        "test_rejected_continuation_never_reaches_discovery",
        "test_request_failures_map_to_fixed_transport_and_http_enums",
        "test_schema_enum_null_bounds_and_complete_semantics",
    }
    expected_new = {
        f"tests/test_inventory_diagnostics.py::InventoryDiagnosticsTests::{name}" for name in names
    }
    expected_collisions = {
        "tests/test_receipt_guard.py::test_invalid_cost_never_allows_another_post[false-nan]": [
            "tests/test_receipt_guard.py::test_invalid_cost_never_allows_another_post[False-NaN]",
            "tests/test_receipt_guard.py::test_invalid_cost_never_allows_another_post[False-nan]",
        ],
        "tests/test_receipt_guard.py::test_invalid_cost_never_allows_another_post[true-nan]": [
            "tests/test_receipt_guard.py::test_invalid_cost_never_allows_another_post[True-NaN]",
            "tests/test_receipt_guard.py::test_invalid_cost_never_allows_another_post[True-nan]",
        ],
    }
    record = {
        "pytest_execution_confirmed": False,
        "collection_hook_reached": False,
        "counts": {"baseline": None, "new": None, "total": None},
        "checks": {},
        "exit_code": None,
        "failure_category": None,
        "failure_stage": None,
        "exception_source": "unknown",
        "exception_class": None,
        "exception_module": None,
        "normalized_top_level_module": None,
    }

    def record_exception(exc, stage):
        cls = type(exc)
        record["failure_stage"] = stage
        record["exception_class"] = safe_exception_class(cls.__name__)
        record["exception_module"] = safe_exception_module(cls.__module__)
        cause = exc.__cause__ or exc.__context__
        if cause is None:
            record["exception_source"] = "direct_exception"
            record["normalized_top_level_module"] = "none"
        else:
            record["exception_source"] = "nested_cause"
            name = getattr(cause, "name", None)
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
                record["normalized_top_level_module"] = safe_top_level_module(name)
            else:
                record["normalized_top_level_module"] = safe_top_level_module(type(cause).__module__)
        record["failure_category"] = (
            "pytest_initial_conftest_import" if stage == "initial_conftests" else "pytest_configuration"
        )

    class CollectionIdentity:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_load_initial_conftests(self, early_config, parser, args):
            try:
                result = yield
                return result
            except BaseException as exc:
                record_exception(exc, "initial_conftests")
                raise

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_configure(self, config):
            try:
                result = yield
                return result
            except BaseException as exc:
                record_exception(exc, "configuration")
                raise

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_sessionstart(self, session):
            record["pytest_execution_confirmed"] = True
            try:
                result = yield
                return result
            except BaseException as exc:
                record_exception(exc, "session_start")
                raise

        def pytest_collection_finish(self, session):
            record["collection_hook_reached"] = True
            nodeids = [item.nodeid for item in session.items]
            counts = Counter(nodeids)
            folded = defaultdict(list)
            for nodeid in sorted(nodeids):
                folded[nodeid.casefold()].append(nodeid)
            collisions = {key: values for key, values in folded.items() if len(values) > 1}
            actual_new = {
                nodeid for nodeid in nodeids if nodeid.startswith("tests/test_inventory_diagnostics.py::")
            }
            baseline = [nodeid for nodeid in nodeids if nodeid not in expected_new]
            baseline_digest = hashlib.sha256(("\n".join(sorted(baseline)) + "\n").encode()).hexdigest()
            checks = {
                "baseline_count": len(baseline) == 294 and len(set(baseline)) == 294
                and len(baseline) == int(os.environ["EXPECTED_BASELINE_TEST_CASES"]),
                "baseline_digest": baseline_digest == os.environ["EXPECTED_BASELINE_NODE_ID_SHA256"],
                "new_id_set": len(expected_new) == 12 and actual_new == expected_new,
                "total_count": len(nodeids) == int(os.environ["EXPECTED_TEST_CASES"])
                and len(counts) == len(nodeids),
                "unique_ids": len(counts) == len(nodeids),
                "casefold_collisions": len(folded) == 304 and collisions == expected_collisions,
                "path_scope": all(nodeid.startswith("tests/") for nodeid in nodeids),
            }
            record["checks"] = checks
            record["counts"] = {"baseline": len(baseline), "new": len(actual_new), "total": len(nodeids)}
            if not all(checks.values()):
                record["failure_category"] = "collection_identity"
                record["failure_stage"] = "collection_finish"
                record["exception_source"] = "direct_exception"
                record["exception_class"] = "AssertionError"
                record["exception_module"] = "builtins"
                record["normalized_top_level_module"] = "none"
                raise AssertionError("collection identity checks failed")

    class Observer:
        def pytest_sessionstart(self, session):
            record["pytest_execution_confirmed"] = True

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = 4
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = int(pytest.main(["tests", "--collect-only", "-q"], plugins=[CollectionIdentity(), Observer()]))
    except BaseException as exc:
        if record["exception_source"] == "unknown":
            record_exception(exc, "pytest_main")
    record["exit_code"] = code
    captured = stdout.getvalue() + "\n" + stderr.getvalue()
    if code != 0 and record["failure_category"] is None:
        if "ConftestImportFailure" in captured or "ImportError while loading conftest" in captured:
            record["failure_category"] = "pytest_import_output"
            record["failure_stage"] = "initial_conftests"
            record["exception_source"] = "output_derived"
            record["exception_class"] = "ConftestImportFailure" if "ConftestImportFailure" in captured else "ImportError"
        else:
            record["failure_category"] = "pytest_usage_exit" if code == 4 else "pytest_nonzero_exit"
            record["failure_stage"] = "pytest_main"
            record["exception_source"] = "exit_code_only"
    if code == 0 and not record["collection_hook_reached"]:
        record["failure_category"] = "collection_hook_not_reached"
        record["failure_stage"] = "pytest_main"
        record["exception_source"] = "exit_code_only"
    return record, code


record = {
    "schema_version": 1,
    "head_sha": os.environ.get("EXPECTED_HEAD_SHA", ""),
    "base_sha": os.environ.get("EXPECTED_BASE_SHA", ""),
    "python_version": sys.version.split()[0],
    "pytest_version": importlib.metadata.version("pytest"),
    "plugin_autoload_enabled": True,
    "collection_only": True,
    "test_bodies_run": False,
    "preflight": {},
    "collection": {},
    "status": "started",
}
atomic_json(DIAG_ROOT / "started-child.json", record)
try:
    record["preflight"] = preflight()
    if not all(record["preflight"].values()):
        record["status"] = "preflight_failed"
        record["collection"] = {
            "pytest_execution_confirmed": False,
            "collection_hook_reached": False,
            "counts": {"baseline": None, "new": None, "total": None},
            "checks": {},
            "exit_code": None,
            "failure_category": "preflight_failed",
            "failure_stage": "preflight",
            "exception_source": "unknown",
            "exception_class": None,
            "exception_module": None,
            "normalized_top_level_module": None,
        }
        exit_code = 1
    else:
        result, exit_code = collection_result()
        record["collection"] = result
        record["status"] = "collection_passed" if exit_code == 0 and all(result["checks"].values()) else "collection_failed"
except BaseException as exc:
    cls = type(exc)
    record["status"] = "diagnostic_error"
    record["collection"] = {
        "pytest_execution_confirmed": False,
        "collection_hook_reached": False,
        "counts": {"baseline": None, "new": None, "total": None},
        "checks": {},
        "exit_code": None,
        "failure_category": "diagnostic_error",
        "failure_stage": "preflight_or_parent",
        "exception_source": "direct_exception",
        "exception_class": safe_exception_class(cls.__name__),
        "exception_module": safe_exception_module(cls.__module__),
        "normalized_top_level_module": None,
    }
    exit_code = 1
finally:
    record["ended_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    atomic_json(DIAG_ROOT / "child-result.json", record)

raise SystemExit(exit_code)
