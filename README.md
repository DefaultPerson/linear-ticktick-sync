# linear-ticktick-sync

Двухсторонний синхронизатор между **Linear** (team `HMC`, project `hm`) и
TickTick-списком **🐍HM&Trade**.

- Linear-issues создаются / обновляются автоматически по правкам в TickTick.
- TickTick-задачи создаются / обновляются автоматически по правкам в Linear.
- Conflict resolution: **last-writer-wins** (по `updatedAt` каждой стороны) +
  hash-канонизация и echo-окна для защиты от self-loop.
- Подзадачи TickTick рендерятся в Linear-description в защищённом fenced-блоке
  (всё внутри блока — наша зона; всё вне его — пользовательское и не трогается).

> v1 ограничения и неподдерживаемые сценарии — см. раздел [Limitations](#limitations).

---

## Stack

- Python 3.12, uv, FastAPI, httpx, SQLAlchemy 2 (aiosqlite), APScheduler, Typer.
- SQLite-state с WAL для durability + idempotency.
- Linear: GraphQL + webhooks (HMAC SHA-256 + replay-protection).
- TickTick: OpenAPI v1 + OAuth 2.0 + 3-минутный poll (webhook'ов нет).

## Project layout
```
src/lt_sync/        # код
  config.py         # pydantic-settings
  app.py            # FastAPI factory + lifespan
  webhook.py        # POST /webhook/linear
  scheduler.py      # APScheduler jobs
  __main__.py       # CLI (lt-sync …)
  state/            # SQLAlchemy models + repo + WAL
  linear/           # GraphQL client + webhook verify
  ticktick/         # OpenAPI client + OAuth + token-provider
  sync/             # mappers, conflict, engine, poller, reconcile, setup
tests/unit/         # mappers + conflict (no I/O)
Dockerfile          # python:3.12-slim multi-stage
docker-compose.yml  # self-host blueprint
.env.example
```

---

## Setup

### 1. Создайте Linear webhook secret
```bash
openssl rand -hex 32
```

### 2. Зарегистрируйте TickTick OAuth-приложение
1. Откройте <https://developer.ticktick.com/manage>, создайте новое приложение.
2. Запишите **Client ID** + **Client Secret**.
3. **Redirect URI** должен совпадать с публичным URL сервиса:
   `https://<ваш-coolify-домен>/oauth/ticktick/callback`.

### 3. Заполните `.env`
Скопируйте `.env.example` → `.env` и заполните:
```
LINEAR_API_KEY=lin_api_…
LINEAR_TEAM_KEY=HMC
LINEAR_PROJECT_NAME=hm
LINEAR_WEBHOOK_SECRET=<openssl rand -hex 32>
TICKTICK_CLIENT_ID=…
TICKTICK_CLIENT_SECRET=…
TICKTICK_REDIRECT_URI=https://<host>/oauth/ticktick/callback
TICKTICK_LIST_ID=69cd04eb8f088eaeff7fb755
PUBLIC_BASE_URL=https://<host>
```

### 4. Подготовьте webhook на стороне Linear
В Linear → Settings → API → Webhooks → New webhook:
- **URL**: `https://<host>/webhook/linear`
- **Resource types**: `Issue`
- **Team**: `HMC`
- **Signing secret**: тот же `LINEAR_WEBHOOK_SECRET`.

---

## Deploy в Coolify (вы делаете сами)

1. Подключите репозиторий в Coolify, создайте сервис «Docker Compose».
2. Загрузите `docker-compose.yml` (или используйте Dockerfile + `lt-sync serve`).
3. Привяжите volume `lt-sync-data:/data` (для `state.db` + `state.db.bak.*`).
4. Поднимите public HTTPS endpoint (Coolify сделает ACME через Traefik).
5. Установите все env-переменные из `.env`.
6. Запустите контейнер.

> Сервис стартует в **degraded mode** до момента TickTick OAuth — webhook-handler
> работает, но poll и upstream writes ждут токена.

---

## OAuth bootstrap (один раз)

После того как сервис поднят:
```bash
# В контейнере или локально:
docker exec -it <container> lt-sync setup ticktick
```
вы увидите authorize URL — откройте его в браузере, авторизуйте TickTick.
Сервис примет callback на `<PUBLIC_BASE_URL>/oauth/ticktick/callback` и сохранит
access_token в SQLite (TTL 180 дней).

Проверьте:
```bash
docker exec -it <container> lt-sync token-status
```

После успешной авторизации scheduler стартует автоматически на следующем перезапуске
сервиса — либо подёргайте контейнер `docker compose restart lt-sync`.

---

## Initial reconciliation

Перед автоматической работой нужно «спарить» существующие Linear-issue с TickTick-задачами:

```bash
# 1. Сухой прогон — формирует TSV-плана пар (rapidfuzz token_set_ratio ≥ 85%
#    + dueDate в пределах 3 дней).
docker exec -it <container> lt-sync match dry-run --out /data/match-plan.tsv

# 2. Откройте /data/match-plan.tsv, вычитайте — там видны actions:
#    link / create_linear / tombstone_linear.
#    Удалите/измените строки которые вы не хотите применять.

# 3. Применить:
docker exec -it <container> lt-sync match confirm /data/match-plan.tsv
```

Эта команда:
- Существующие пары — записывает Link, переносит Linear-issue в Project `hm`,
  добавляет label `ticktick-sync`, инжектирует fenced-блок с подзадачами.
- TT-задачи без пары — создаёт новые Linear-issues.
- Linear-issues с лейблом `ticktick-sync` без пары — переводит в `Noted` + label
  `tombstoned-from-ticktick`.

После reconciliation сервис уже работает в фоне — изменения с любой стороны
будут синхронизироваться.

---

## Operational notes

- **Логи**: structlog → JSON в продакшне (TTY → pretty в dev).
- **Healthcheck**: `GET /healthz` возвращает `{ok, ctx_ready, scheduler_running}`.
- **Poll-once вручную**: `lt-sync poll-once` (для отладки без scheduler).
- **Backup state.db**: рекомендую cron в Coolify раз в сутки —
  `cp /data/state.db /data/state.db.bak.$(date +%F)`.
- **Token expiry alerts**: если выставите `PUSHOVER_TOKEN`+`PUSHOVER_USER`,
  получите уведомления за 7 дней и за 1 день до истечения 180-дневного TTL.

## Limitations (v1)

- TickTick → Linear для подзадач: одностороннее. Чек-лист в Linear не пушится обратно в TT.
- Tags TickTick ↔ labels Linear не синхронизируются (только колонка `📦 Делегировано` → label `Delegated`).
- Comments / attachments не синхронизируются (TT OpenAPI не поддерживает).
- При удалении Linear-issue TT-задача переводится в `wontDo`, не удаляется.
- TT-задача удалена → после двух подряд пропусков poll'а Linear-issue переводится в `Noted` + label `tombstoned-from-ticktick`.
- Sub-states Linear (`In Progress`, `In Review`, `Ready to deploy`, `Soon`, `New`, `Later`) в TT не различаются — в TT всё это `status=0`. На обратном пути сохраняется существующий sub-state.
- Linear «Urgent» (priority=1) в TT нет; при write Linear→TT шлём `5` (high), но при TT→Linear «Urgent» сохраняется.

## Tests

```bash
uv run pytest tests/unit -v       # mappers + conflict (без сети)
uv run pytest -m "not e2e"        # всё кроме e2e
uv run pytest -m e2e              # требуют живые credentials
```

## License

MIT.
