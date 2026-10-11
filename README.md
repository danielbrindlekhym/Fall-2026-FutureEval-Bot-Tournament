# Fall 2026 FutureEval bot

An autonomous Metaculus forecasting bot targeting a top-half seasonal finish. This is an objective, not a performance guarantee.

The bot reads each complete question and its rules, retrieves bounded public evidence, asks up to two free models for independent estimates, validates and pools their distributions, and submits one forecast with private reasoning. It uses `forecasting-tools` for numeric/discrete distributions.

## Cost policy

Personal spending ceiling: **$0**. Explicit `:free` model IDs, freshly verified zero pricing, zero-price provider limits, no paid plugins or paid fallback, a persistent daily request cap, and receipt checks enforce this policy. Donated credits requested from the organizer are pending and do not alter the policy.

## Setup

Python 3.12 and the dependencies in requirements.txt are sufficient. No GPU or model training is required.

1. Install dependencies with `python -m pip install -r requirements.txt`.
2. Keep METACULUS_TOKEN and OPENROUTER_API_KEY in a private .env locally, or this repository's GitHub Actions secrets. Never commit them.
3. Run `python scripts/verify.py`, then `python -m futureeval_bot record-offline-checks`.
4. Run `python -m futureeval_bot practice`. Confirm all four formats are verified in status.
5. For hosted operation, create the dedicated `bot-state` branch and seed its `progress.enc` file using `python -m futureeval_bot.cloud export` from verified local progress. Never upload the plaintext database.
6. Run the **FutureEval bot** workflow manually in doctor mode. Once registration, practice, and hosted recovery are verified, set live_enabled in config.json to true and the repository variable FUTUREEVAL_SCHEDULE_ENABLED to true.

The workflow runs on standard public GitHub runners at minutes 7, 27 and 47. GitHub may delay scheduled runs; the 95% coverage target is not guaranteed. A single concurrency group prevents overlapping workers.

## Durable progress

The encrypted database lives on `bot-state`, separate from source. Each model call reservation and frozen forecast is checkpointed before the external action. Updates use GitHub's expected file SHA to reject conflicting writes. A missing, corrupt, or unavailable checkpoint stops operation. Plaintext reasoning and credentials are never included in public workflow logs or uploaded artifacts.

Checkpoint encryption is derived from the private Metaculus token. Before rotating that token, preserve a decrypted local backup using the old token and re-encrypt it with the new token; otherwise old checkpoints cannot be opened. GitHub's automatic workflow token only needs write access to this repository. Do not expose secrets to pull request workflows or untrusted contributors.

## Operation

`python -m futureeval_bot status` reports coverage, phases, verified formats and reported costs. Run `doctor` to check accounts and free model availability. `live` refuses to start without registration confirmation, four practice formats, fresh offline evidence, and explicit live enablement. Changes to source/configuration invalidate old offline evidence.

Inspect operational summaries and failures, not individual live forecasts. Use only permitted resolved/practice data for improvements. See NOTICE.md for upstream references.
