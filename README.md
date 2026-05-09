# linear-ticktick-sync

Two-way synchronizer between **Linear** (one team + project) and a single **TickTick** list.

- Edits on either side propagate within ~3 minutes.
- Conflict resolution: last-writer-wins by `updatedAt` + canonical hashes + 30 s echo windows to prevent loops.
- TickTick checklist (`items[]`) is rendered inside a fenced block in the Linear description; everything outside the fence is yours and untouched.

> See [Limitations](#limitations) for the supported feature surface.

## Stack

Python 3.12 · uv · FastAPI · httpx · SQLAlchemy 2 (aiosqlite) · APScheduler · Typer · Docker.

## Setup

1. Copy `.env.example` → `.env` and fill in credentials:
   - `LINEAR_API_KEY` — Linear PAT.
   - `LINEAR_TEAM_KEY` — team key (e.g. `HMC`).
   - `LINEAR_PROJECT_NAME` — project name (auto-created if missing).
   - `LINEAR_WEBHOOK_SECRET` — `openssl rand -hex 32`.
   - `TICKTICK_CLIENT_ID` / `TICKTICK_CLIENT_SECRET` — register an app at <https://developer.ticktick.com/manage>.
   - `TICKTICK_REDIRECT_URI` — `https://<your-host>/oauth/ticktick/callback`.
   - `TICKTICK_LIST_ID` — the TickTick project (list) id you want to mirror.
2. Configure a Linear webhook (Settings → API → Webhooks):
   - URL: `https://<your-host>/webhook/linear`
   - Resource: `Issue`
   - Signing secret: same as `LINEAR_WEBHOOK_SECRET`.
3. Build and run:
   ```bash
   docker compose up -d --build
   ```
   Mount `/data` as a persistent volume — `state.db` lives there.
4. Authorize TickTick OAuth:
   ```bash
   docker compose exec lt-sync lt-sync setup ticktick   # prints authorize URL
   # open the URL, authorize; the callback stores the token in state.db
   docker compose exec lt-sync lt-sync token-status     # verify
   docker compose restart lt-sync                       # scheduler picks up token
   ```
5. Initial reconciliation — pair existing items:
   ```bash
   docker compose exec lt-sync lt-sync match dry-run --out /data/match-plan.tsv
   # review the TSV (link / create_linear / tombstone_linear)
   docker compose exec lt-sync lt-sync match confirm /data/match-plan.tsv
   ```

After this, the service runs autonomously: webhooks from Linear and the 3-minute TickTick poll keep both sides in sync.

## CLI

```
lt-sync serve              # FastAPI service (webhook + scheduler)
lt-sync setup ticktick     # print TickTick OAuth authorize URL
lt-sync token-status       # show TickTick token expiry
lt-sync match dry-run      # build match plan TSV (no writes)
lt-sync match confirm <plan.tsv>
lt-sync poll-once          # one-shot poll (for debugging)
```

## Tests

```bash
uv run ruff check src/ tests/
uv run pytest tests/unit -q     # 30 unit tests, no network
```

## Limitations (v1)

- TickTick → Linear is one-way for subtasks. Editing the checklist in Linear is not pushed back.
- Tags, comments and attachments are not synced (TickTick OpenAPI does not expose them).
- Linear sub-states (`In Progress`, `In Review`, `Ready to deploy`, `Soon`, `New`, `Later`) collapse to TickTick `status=0`. The existing Linear sub-state is preserved on inbound TT events.
- Linear `Urgent` (priority=1) has no TickTick counterpart; it is preserved on Linear and emitted as TT `5` (high) on outbound writes.
- Deleting a Linear issue marks the TickTick task `wontDo` (does not delete it).
- A TickTick task missing from two consecutive polls (~6 min) marks the Linear issue `Noted` + label `tombstoned-from-ticktick` (does not delete it).
- TickTick column moves are read-only (the public OpenAPI does not expose column writes).

## License

MIT.
