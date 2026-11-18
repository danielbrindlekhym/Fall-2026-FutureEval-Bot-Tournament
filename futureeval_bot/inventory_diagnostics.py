"""Privacy-safe fixed-schema inventory diagnostics for a single hosted operation."""
from __future__ import annotations

import json

SCHEMA_VERSION = 1
FAILURE_SUBTYPES = frozenset({
    "none", "transport_timeout", "transport_connection", "transport_other", "http_auth",
    "http_not_found", "http_rate_limited", "http_server_exhausted", "http_other",
    "response_top_level_shape", "response_results_shape", "response_post_shape",
    "response_question_shape", "continuation_missing", "continuation_fragment",
    "continuation_query_missing", "continuation_origin_or_path", "continuation_query_syntax",
    "continuation_offset_invalid", "continuation_filter_mismatch",
    "continuation_repeated_or_nonadvancing", "page_limit_exhausted",
    "supported_metadata_invalid", "state_read_failed", "state_write_failed",
    "other_inventory", "unknown",
})
DISPOSITIONS = frozenset({
    "not_started", "terminal_no_next", "next_validated", "continuation_rejected",
    "request_failed", "response_shape_failed", "page_limit_reached", "unknown",
})
DIAGNOSTIC_KEYS = frozenset({
    "schema_version", "status", "failure_subtype", "pagination_disposition",
    "pages_completed", "items_received", "last_page_items", "pagination_complete",
})
BINDING_KEYS = frozenset({
    "operation_id", "repository", "workflow_path", "workflow_run_id",
    "workflow_run_attempt", "source_sha", "config_fingerprint", "started_at",
})
UNKNOWN_DIAGNOSTICS = {
    "schema_version": SCHEMA_VERSION, "status": "unknown", "failure_subtype": "unknown",
    "pagination_disposition": "unknown", "pages_completed": None,
    "items_received": None, "last_page_items": None, "pagination_complete": None,
}


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def validate_diagnostics(value):
    if not isinstance(value, dict) or set(value) != DIAGNOSTIC_KEYS:
        return None
    if type(value.get("schema_version")) is not int or value.get("schema_version") != SCHEMA_VERSION:
        return None
    if not isinstance(value.get("status"), str) or value.get("status") not in {"complete", "failed", "partial", "unknown"}:
        return None
    if not isinstance(value.get("failure_subtype"), str) or value.get("failure_subtype") not in FAILURE_SUBTYPES:
        return None
    if not isinstance(value.get("pagination_disposition"), str) or value.get("pagination_disposition") not in DISPOSITIONS:
        return None
    pages, items, last = (value.get("pages_completed"), value.get("items_received"),
                          value.get("last_page_items"))
    if pages is not None and not _integer(pages, 30):
        return None
    if items is not None and not _integer(items, 3000):
        return None
    if last is not None and not _integer(last, 100):
        return None
    if value.get("pagination_complete") is not None and type(value.get("pagination_complete")) is not bool:
        return None
    if items is not None and pages is not None and items > pages * 100:
        return None
    if last is not None and items is not None and last > items:
        return None
    if pages == 0 and ((items is not None and items != 0) or (last is not None and last != 0)):
        return None
    status, subtype, disposition, complete = (
        value["status"], value["failure_subtype"], value["pagination_disposition"],
        value["pagination_complete"])
    if status == "complete":
        if (subtype != "none" or disposition != "terminal_no_next" or complete is not True
                or not _integer(pages, 30) or pages < 1 or not _integer(items, 3000)
                or not _integer(last, 100)):
            return None
    elif status == "unknown":
        if (subtype != "unknown" or disposition != "unknown" or complete is not None
                or pages is not None or items is not None or last is not None):
            return None
    elif subtype == "none":
        return None
    if status == "partial" and complete is True:
        return None
    if disposition == "continuation_rejected" and complete is not False:
        return None
    if complete is True and disposition != "terminal_no_next":
        return None
    return {key: value[key] for key in (
        "schema_version", "status", "failure_subtype", "pagination_disposition",
        "pages_completed", "items_received", "last_page_items", "pagination_complete")}


def summary_diagnostics(raw, expected_binding, operation_status):
    """Project only schema fields after exact operation binding; fail closed to unknown."""
    try:
        record = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(record, dict) or set(record) != {"binding", "diagnostics"}:
            return dict(UNKNOWN_DIAGNOSTICS)
        binding = record["binding"]
        if not isinstance(binding, dict) or set(binding) != BINDING_KEYS:
            return dict(UNKNOWN_DIAGNOSTICS)
        if not isinstance(expected_binding, dict) or binding != expected_binding:
            return dict(UNKNOWN_DIAGNOSTICS)
        clean = validate_diagnostics(record["diagnostics"])
        if clean is None:
            return dict(UNKNOWN_DIAGNOSTICS)
        if clean["status"] == "complete" and operation_status != "completed":
            return dict(UNKNOWN_DIAGNOSTICS)
        return clean
    except (TypeError, ValueError, json.JSONDecodeError):
        return dict(UNKNOWN_DIAGNOSTICS)


class InventoryDiagnostics:
    """Best-effort persistence; instrumentation failure never masks inventory outcome."""

    def __init__(self, state):
        self.state = state
        self.binding = None
        self.key = None
        self.remote = None
        self.progress = dict(UNKNOWN_DIAGNOSTICS)
        try:
            operation_id = state.active_operation_id
            if operation_id:
                row = state.db.execute(
                    "SELECT operation_id,repository,workflow_path,workflow_run_id,workflow_run_attempt,"
                    "source_sha,config_fingerprint,started_at,status FROM operations WHERE operation_id=?",
                    (operation_id,)).fetchone()
                if row is not None and row["status"] == "started":
                    self.binding = {key: row[key] for key in BINDING_KEYS}
                    self.key = "inventory_diagnostics:" + operation_id
        except Exception:
            self.binding = self.key = None
        self._save(UNKNOWN_DIAGNOSTICS)

    def attach(self, remote):
        self.remote = remote

    def _progress(self):
        try:
            if self.remote is not None:
                progress = self.remote.inventory_progress()
                if isinstance(progress, dict):
                    return progress
        except Exception:
            pass
        return {}

    def _save(self, value):
        clean = validate_diagnostics(value)
        if self.binding is None or self.key is None or clean is None:
            return
        try:
            self.state.set_meta(self.key, {"binding": self.binding, "diagnostics": clean})
        except Exception:
            # Missing/failed persistence is rendered as unknown by the summary reader.
            pass

    def failed(self, subtype="unknown", *, disposition=None, progress=None):
        if subtype not in FAILURE_SUBTYPES or subtype == "none":
            subtype = "unknown"
        p = progress if isinstance(progress, dict) else self._progress()
        current_disposition = disposition or p.get("pagination_disposition", "unknown")
        if current_disposition not in DISPOSITIONS:
            current_disposition = "unknown"
        pages, items, last = p.get("pages_completed"), p.get("items_received"), p.get("last_page_items")
        pagination_complete = p.get("pagination_complete")
        if current_disposition == "continuation_rejected":
            pagination_complete = False
        if type(pagination_complete) not in (bool, type(None)):
            pagination_complete = None
        record = {
            "schema_version": 1, "status": "failed", "failure_subtype": subtype,
            "pagination_disposition": current_disposition, "pages_completed": pages,
            "items_received": items, "last_page_items": last,
            "pagination_complete": pagination_complete,
        }
        if validate_diagnostics(record) is None:
            record.update(pages_completed=None, items_received=None, last_page_items=None,
                          pagination_disposition="unknown", pagination_complete=None)
            if subtype == "none":
                record["failure_subtype"] = "unknown"
        self.progress = record
        self._save(record)

    def complete(self):
        p = self._progress()
        record = {
            "schema_version": 1, "status": "complete", "failure_subtype": "none",
            "pagination_disposition": p.get("pagination_disposition"),
            "pages_completed": p.get("pages_completed"), "items_received": p.get("items_received"),
            "last_page_items": p.get("last_page_items"), "pagination_complete": p.get("pagination_complete"),
        }
        if validate_diagnostics(record) is None:
            self.failed("unknown")
        else:
            self.progress = record
            self._save(record)


def collect_inventory(remote, project, state, config, diagnostics, *, supported,
                      contract_factory, is_future, service_error, on_invalid_post):
    """Complete the full page chain and discovery transaction before returning any work."""
    diagnostics.attach(remote)
    try:
        posts = remote.posts(project)
    except Exception as error:
        subtype = getattr(error, "failure_subtype", None)
        disposition = getattr(error, "pagination_disposition", None)
        progress = remote.inventory_progress() if hasattr(remote, "inventory_progress") else None
        if disposition == "continuation_rejected":
            progress = {
                "pages_completed": getattr(error, "pages_completed", None),
                "items_received": getattr(error, "items_received", None),
                "last_page_items": getattr(error, "last_page_items", None),
                "pagination_disposition": disposition, "pagination_complete": False,
            }
        if not subtype:
            subtype = getattr(remote, "inventory_failure_subtype", "unknown")
        if not disposition:
            disposition = getattr(remote, "inventory_pagination_disposition", None)
        diagnostics.failed(subtype, disposition=disposition, progress=progress)
        raise
    contracts, skipped = {}, 0
    for post in posts:
        if not isinstance(post, dict):
            diagnostics.failed("response_post_shape", disposition="response_shape_failed")
            raise service_error("Inventory post shape invalid")
        question = post.get("question")
        if question is not None and not isinstance(question, dict):
            diagnostics.failed("response_question_shape", disposition="response_shape_failed")
            raise service_error("Inventory question shape invalid")
        q = question or {}
        if q.get("type") not in supported:
            skipped += 1
            continue
        try:
            contract = contract_factory(post, project)
        except (ValueError, KeyError, TypeError):
            diagnostics.failed("supported_metadata_invalid")
            try:
                on_invalid_post(post)
            except Exception:
                # Optional telemetry must not replace the primary metadata failure.
                pass
            raise service_error("Supported question metadata failed validation") from None
        if is_future(contract):
            continue
        try:
            state.discover(contract, config)
        except Exception:
            diagnostics.failed("state_write_failed")
            raise
        contracts[contract.question_id] = contract
    diagnostics.complete()
    return posts, contracts, skipped

