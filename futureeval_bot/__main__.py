from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from dotenv import load_dotenv

from .api import FreeModel, Metaculus, ServiceError, reconcile_and_submit
from .core import SUPPORTED, Config, Contract, State, canonical, run_lock, source_hashes, timestamp, utcnow
from .forecast import NoValidatedForecast, prepare
from .research import research
from .cloud import configured_cloud
from .inventory import release_inventory, inventory_counts


def services(config: Config, state: State):
    remote = Metaculus(os.getenv("METACULUS_TOKEN", ""))
    identity = remote.identity(config.expected_bot_user_id)
    model = FreeModel(config, state, os.getenv("OPENROUTER_API_KEY", ""))
    checks = model.check()
    state.set_meta("service_checks", {"at": utcnow().isoformat(), "identity": identity, "openrouter": checks})
    return remote, model


def live_gate(config: Config, state: State):
    problems = []
    if not config.registration_confirmed:
        problems.append("Participation form has not been confirmed complete")
    if not config.live_enabled:
        problems.append("Live operation is disabled in config.json")
    formats = set(state.meta("practice_verified_formats", []))
    if not SUPPORTED <= formats:
        problems.append("End-to-end practice verification is incomplete")
    report = state.meta("offline_checks_passed", {})
    if not report.get('passed') or report.get('config_fingerprint') != config.fingerprint or report.get('source_hashes') != source_hashes():
        problems.append("Recorded offline reliability checks are missing or stale")
    if state.meta("cost_guard_halt"):
        problems.append("Zero-cost guard needs review")
    if problems:
        raise ServiceError("; ".join(problems))


def run(config: Config, state: State, remote, model, *, practice=False, max_questions=None,
        workflow_run_id=None, workflow_run_attempt=None, operation_started_at=None) -> dict:
    project = config.practice_project if practice else config.season_project
    if not practice:
        live_gate(config, state)
    # Reconcile the whole feed, including closed questions missed during downtime.
    posts = remote.posts(project)
    feed_checked_at = utcnow().isoformat()
    contracts, skipped = {}, 0
    for post in posts:
        q = post.get("question") or {}
        if q.get("type") not in SUPPORTED:
            skipped += 1
            continue
        try:
            contract = Contract.from_post(post, project)
            if contract.status == "upcoming" or (contract.open_time and timestamp(contract.open_time) > utcnow()):
                continue
            state.discover(contract, config)
            contracts[contract.question_id] = contract
        except (ValueError, KeyError, TypeError):
            state.set_meta("feed_parse_error_" + str(post.get("id")), {"at": utcnow().isoformat(), "post_id": post.get("id")})
            raise ServiceError("Supported question metadata failed validation; see saved post identifier") from None

    if practice:
        # Choose one fixed available example of each format, not whichever produces a desired answer.
        selected = {}
        for contract in sorted(contracts.values(), key=lambda c: (c.close_time, c.question_id)):
            if contract.forecastable():
                selected.setdefault(contract.kind, contract.question_id)
        wanted = set(selected.values())
    else:
        wanted = set(contracts)
        if not state.meta("launch_time"):
            state.set_meta("launch_time", utcnow().isoformat())
            state.set_meta("launch_config", asdict(config))
    work = []
    for job in state.jobs(project):
        if job["phase"] in {"verified", "external_forecast", "missed"}:
            continue
        contract = contracts.get(job["question_id"], Contract(**json.loads(job["contract"])))
        if job["payload"]:
            work.append((0, contract, job))
        elif contract.question_id in wanted:
            work.append((1, contract, job))
    work.sort(key=lambda item: (item[0], timestamp(item[1].close_time), item[1].question_id))
    attempted, failures = 0, []
    for _, contract, job in work:
        if attempted >= (max_questions or config.max_questions_per_run):
            break
        try:
            if job["payload"]:
                reconcile_and_submit(state, remote, contract.question_id)
                attempted += 1
                continue
            # Configurations are frozen per question, so a blocked model set can
            # defer this job without suppressing a later job with other free models.
            job_config = Config(**json.loads(job["config"]))
            if model.rate_limit_circuit_open(job_config):
                model.rate_limit_circuit_opened = True
                continue  # Defer this job while still reconciling later frozen payloads.
            if not contract.forecastable() or contract.remaining() <= config.deadline_reserve_seconds:
                state.update(contract.question_id, phase="missed", error="Insufficient time before close")
                continue
            # Respect a pre-existing server submission even if local state was lost.
            if remote.forecasts(contract.question_id):
                state.update(contract.question_id, phase="external_forecast", error="Server forecast predates saved payload; skipped")
                continue
            attempted += 1
            # Freeze each job's configuration from initial discovery.
            previous_config = model.config
            model.config = job_config
            seconds = min(job_config.question_budget_seconds, contract.remaining() - job_config.deadline_reserve_seconds)
            stop_at = time.monotonic() + seconds
            if job["research"]:
                evidence = json.loads(job["research"])
            else:
                clarifications = remote.comments(contract.post_id, staff=True)
                contract = replace(contract, staff_clarifications=[c.get("text", "") for c in clarifications])
                evidence = research(contract, job_config, min(stop_at - 30, time.monotonic() + 75))
                evidence["staff_clarifications"] = contract.staff_clarifications
                state.update(contract.question_id, research=canonical(evidence), phase="researched")
            prepare(state, contract, job_config, model, evidence, stop_at)
            model.config = previous_config
            reconcile_and_submit(state, remote, contract.question_id)
        except Exception as error:
            if 'previous_config' in locals():
                model.config = previous_config
            # No raw model answer or source text goes to workflow logs.
            state.update(contract.question_id, error=type(error).__name__ + ": " + str(error)[:220])
            failure = {"question_id": contract.question_id, "error_type": type(error).__name__}
            if isinstance(error, NoValidatedForecast):
                failure["diagnostics"] = error.diagnostics
            failures.append(failure)
        print(canonical({"question_id": contract.question_id, "phase": state.job(contract.question_id)["phase"]}), flush=True)

    if practice:
        verified_formats = {Contract(**json.loads(job["contract"])).kind for job in state.jobs(project) if job["phase"] == "verified"}
        state.set_meta("practice_verified_formats", sorted(verified_formats))
    previous_feed = state.meta('full_feed_reconciled', {})
    try:
        if not isinstance(previous_feed, dict) or not isinstance(previous_feed.get('release_inventories', {}), dict):
            raise ValueError('Saved release inventory container changed')
        inventories = dict(previous_feed.get('release_inventories', {}))
        inventory = release_inventory(posts, getattr(remote, 'feed_pages', []), feed_checked_at, inventories.get(str(project)))
        export_counts = inventory_counts(inventory)
    except ValueError:
        # Optional telemetry must not halt forecasting or overwrite evidence needing review.
        export_counts = {'status':'unavailable', 'reason':'metadata_validation_failed',
            'complete_official_released_inventory':False}
    else:
        inventories[str(project)] = inventory
        # Durable writes remain outside the optional-validation exception handler.
        state.set_meta('full_feed_reconciled', {'at': utcnow().isoformat(), 'project': project,
            'posts': len(posts), 'release_inventories': inventories})
    state.set_meta("last_run", {"at": utcnow().isoformat(), "project": project, "config": config.fingerprint,
        "failures": failures, "questions_considered": attempted, "unsupported_posts": skipped,
        "workflow_run_id": workflow_run_id, "workflow_run_attempt": workflow_run_attempt,
        "operation_started_at": operation_started_at})
    result = state.summary(project)
    result["release_inventory_counts"] = export_counts
    result["rate_limit_circuit_open"] = bool(getattr(model, "rate_limit_circuit_opened", False))
    result["failures"] = failures
    result["practice_verified_formats"] = state.meta("practice_verified_formats", [])
    return result


def main():
    parser = argparse.ArgumentParser(description="Zero-dollar autonomous FutureEval bot")
    parser.add_argument("mode", choices=["doctor", "practice", "live", "status", "record-offline-checks"])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--max-questions", type=int)
    args = parser.parse_args()
    load_dotenv()
    config = Config.load(args.config)
    with run_lock(config.state_dir):
        state = State(Path(config.state_dir) / "bot.db")
        try:
            cloud = configured_cloud()
            if cloud is not None:
                cloud.attach(state)
            if args.mode == "status":
                print(canonical(state.summary()))
            elif args.mode == "record-offline-checks":
                report = json.loads(Path("outputs/offline-verification.json").read_text(encoding="utf-8"))
                if not report.get("passed") or report.get("config_fingerprint") != config.fingerprint or report.get('source_hashes') != source_hashes():
                    raise ServiceError("Offline verification report is missing, failed, or stale")
                state.set_meta("offline_checks_passed", report)
                print("Recorded passing offline verification.")
            elif args.mode == "doctor":
                remote, model = services(config, state)
                print(canonical(state.meta("service_checks")))
                print(canonical({"registration_confirmed": config.registration_confirmed, "live_enabled": config.live_enabled,
                    "practice_verified_formats": state.meta("practice_verified_formats", [])}))
            else:
                if args.mode == "live":
                    live_gate(config, state)
                remote, model = services(config, state)
                workflow_run_id = os.getenv("BOT_WORKFLOW_RUN_ID", "")
                workflow_run_attempt = os.getenv("BOT_WORKFLOW_RUN_ATTEMPT", "")
                operation_started_at = os.getenv("BOT_OPERATION_STARTED_AT", "")
                result = run(config, state, remote, model, practice=args.mode == "practice", max_questions=args.max_questions,
                    workflow_run_id=workflow_run_id if workflow_run_id.isdigit() else None,
                    workflow_run_attempt=workflow_run_attempt if workflow_run_attempt.isdigit() else None,
                    operation_started_at=operation_started_at or None)
                print(canonical(result))
                if result["failures"]:
                    return 1
        finally:
            state.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ServiceError, ValueError, OSError) as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        sys.exit(2)
