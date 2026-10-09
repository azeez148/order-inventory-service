# Order & Inventory Service

An asynchronous FastAPI backend for a shared order and inventory service.
The current implementation includes the FastAPI scaffold and a Docker development
environment with PostgreSQL and Meilisearch, plus the initial database schema.
Application database integration,
product, order, search, and reporting functionality will follow incrementally.

## Run with Docker Compose

Requires Docker Engine, the current Docker Compose CLI plugin (`docker compose`),
and Docker Buildx for image builds. Docker Desktop includes these tools.
See [Docker's plugin installation instructions](https://docs.docker.com/compose/install/linux/)
if the command is unavailable; legacy `docker-compose` is not the documented path.
Run from the repository root:

```bash
cp .env.example .env
docker compose up --build -d --wait
docker compose ps
```

Open <http://localhost:8000/docs> or <http://localhost:8000/health>.
Meilisearch health is available at <http://localhost:7700/health>. App and search
ports bind to loopback for local development. PostgreSQL is accessible inside
Compose as `postgres:5432`; it does not publish a host port.

Verify an actual PostgreSQL connection:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT current_database(), current_user;"'
```

Containers have health checks, and the app starts after PostgreSQL and Meilisearch
are healthy. At this step the app only serves health/docs; it does not yet open
database connections or call search. PostgreSQL creates the four application
tables from `schema.sql` when its data volume is initialized for the first time.

`.env` is ignored by Git. Its example password and search key are explicit local
development placeholders. If changing PostgreSQL credentials, update
`DATABASE_URL` consistently (URL-encode credentials containing reserved characters).
PostgreSQL initialization variables apply when its data volume is first created;
changing them later does not change an existing database user's password.

View logs or stop the stack while preserving data:

```bash
docker compose logs --tail=100
docker compose down
```

For a fresh local database and search index, `docker compose down -v` deletes
this project's data volumes. Then rerun the startup command.

## Database schema

`schema.sql` creates `products`, `orders`, `order_items`, and `search_outbox` in
one transaction through PostgreSQL's Docker initialization directory. Prices and
totals use exact `NUMERIC` values; stock cannot be negative, quantities must be
positive, and foreign keys protect order and outbox references. Each order stores
its item unit prices so later product-price changes do not alter historical orders.
Product/order deletion is restricted while referenced; there are no deletion APIs.

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"'
```

Initialization scripts only run on an empty PostgreSQL data volume. If upgrading
the empty Step 2 database, apply the schema once after `docker compose up -d --wait`:

```bash
docker compose exec postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/001-schema.sql'
```

Do not rerun this command on an already initialized schema. Schema changes require
migrations or an intentional local reset; this script is not a migration system.
Alembic is the planned production evolution. `updated_at` starts with a default
timestamp and will be maintained by repository updates in later steps.

## Planned database connection strategy

PostgreSQL provides ACID transactions, constraints, relational integrity, and
atomic inventory updates. The baseline will connect directly through SQLAlchemy
async and asyncpg; PgBouncer remains optional. `.env.example` reserves Step 4
settings of five pooled connections, five overflow connections, and a ten-second
pool acquisition timeout per application process. A maximum of ten OLTP connections
is a conservative starting budget for a single local worker; these settings are
not active yet and will be validated under load. Reporting will receive separate,
smaller connection capacity later.

Ordinary SQLAlchemy persistence can remain reasonably portable. Concurrency SQL
will be isolated behind repository methods; moving to MySQL or Oracle requires
adapting those implementations, drivers, and schema rather than just changing the
connection string.

## Run locally

Requires Python 3.12 and pip. Run these commands from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload
```

- Health: <http://localhost:8000/health> returns `{"status":"ok"}`.
- Swagger/OpenAPI interface: <http://localhost:8000/docs>.
- OpenAPI schema: <http://localhost:8000/openapi.json>.

The health endpoint is a liveness check; it does not check external dependencies.
Frontend UI is explicitly out of scope. Swagger provides manual API interaction.

## Structure

Routes in `app/api` handle HTTP behavior, with Pydantic models in `app/schemas`.
Future business rules belong in `app/services`; persistence and search details
belong in `app/repositories`, `app/db`, and `app/search`. Shared configuration
belongs in `app/core`. These packages establish small boundaries without adding
a generic framework or unnecessary business layers to the health endpoint.

See [Implementation Decisions](docs/IMPLEMENTATION_DECISIONS.md) for the
assessment, rationale, validation evidence, and remaining limitations.
