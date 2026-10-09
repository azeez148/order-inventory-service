# Order & Inventory Service

An asynchronous FastAPI backend for a shared order and inventory service.
The current implementation includes the FastAPI scaffold and a Docker development
environment with PostgreSQL and Meilisearch, plus the initial database schema.
The app connects through a bounded asynchronous SQLAlchemy pool and supports
product creation, retrieval, metadata updates, and atomic stock adjustments.
Order, search, and reporting functionality will follow incrementally.

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
are healthy. The app checks PostgreSQL connectivity at startup and then serves
health/docs and product/inventory endpoints. Search calls are not implemented yet.
PostgreSQL creates the four application
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
Alembic is the planned production evolution. Repositories maintain `updated_at`
when changing product metadata or stock.

## Database connection strategy

PostgreSQL provides ACID transactions, constraints, relational integrity, and
atomic inventory updates. The baseline connects directly through SQLAlchemy
async and asyncpg; PgBouncer remains optional. Settings are read from the process
environment and validated at startup:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | Required | PostgreSQL URL using the `postgresql+asyncpg` driver |
| `DB_POOL_SIZE` | `5` | Maximum retained connections; connections open on demand |
| `DB_MAX_OVERFLOW` | `5` | Additional connections allowed during a burst |
| `DB_POOL_TIMEOUT` | `10` seconds | Maximum wait to acquire a connection when saturated |

The ten-connection OLTP cap is a conservative starting budget for one local
worker. Requests beyond the cap wait for a returned connection, rather than
opening unlimited PostgreSQL sessions. Pool timeout limits that wait; it is not a
query timeout. Overflow connections close when returned. `pool_pre_ping=True`
checks connections on checkout and replaces stale connections; it does not retry
failed transactions. These settings will also be evaluated in later load tests.

One engine/session factory is created per app lifespan. Startup executes
`SELECT 1` and fails if PostgreSQL cannot be reached. Request-scoped sessions use
an async context manager to return connections and roll back unfinished work.
Services must explicitly manage write transactions; the dependency does not
automatically commit. Shutdown disposes the engine. An `AsyncSession` belongs to
one request/task and must not be shared among concurrent tasks.

Pools are per process: four workers at these settings could open up to 40 OLTP
connections, before future reporting capacity or other clients. Reporting will
receive a separate smaller pool later. PgBouncer can provide centralized
PostgreSQL connection management when worker/replica counts justify it.

Ordinary SQLAlchemy persistence can remain reasonably portable. Concurrency SQL
will be isolated behind repository methods; moving to MySQL or Oracle requires
adapting those implementations, drivers, and schema rather than just changing the
connection string.

Validate database lifecycle and pooling against the running Compose database:

```bash
docker compose exec -T app python < scripts/check_db_pool.py
```

The script asserts configuration validation, pool saturation/recovery,
uncommitted-write rollback after normal exit/errors/cancellation, stale-connection
replacement, and shutdown cleanup. Its fixture writes are rolled back. It
terminates only a PostgreSQL connection opened by the script to test pre-ping;
the database user must be allowed to terminate its own sessions. A small test
pool with a short timeout keeps saturation checks quick without changing the
running app's configured pool.

See [SQLAlchemy's async lifecycle documentation](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
and [pooling documentation](https://docs.sqlalchemy.org/en/20/core/pooling.html)
for the underlying connection behavior.

## Product and inventory APIs

| Method | Endpoint | Behavior |
|---|---|---|
| POST | `/api/v1/products` | Create product; returns 201 |
| GET | `/api/v1/products/{id}` | Retrieve authoritative product data |
| PATCH | `/api/v1/products/{id}` | Update name, description, or price |
| POST | `/api/v1/products/{id}/stock` | Apply a signed stock adjustment |

In Swagger, create a product with:

```json
{"name":"Football","description":"Training ball","price":"19.99","stock":10}
```

Use the returned ID to retrieve it, patch its description, then submit
`{"adjustment":25}` to its stock endpoint. A negative adjustment removes stock;
an adjustment exceeding available stock returns 409 without changing the row.
Missing products return 404, invalid requests return 422, and connection pool
acquisition timeout returns 503 with a generic message.

Money is parsed as Decimal and returned as a decimal string. Prices must be
nonnegative with at most two decimal places and fit `NUMERIC(12,2)`. Stock must
fit PostgreSQL's nonnegative integer range. Names are trimmed and limited to
255 characters; descriptions are limited to 10,000 characters. Unknown request
fields are rejected. PATCH cannot change stock; empty patches and explicit nulls
are rejected. Adjustments must be nonzero integers. Metadata no-ops preserve the
timestamp and do not generate extra events.

Creation and actual name/description changes append an `upsert` outbox event in
the same transaction as the product write. A failed event insert rolls back the
product change. Price-only and stock-only changes need no text-search event:
search will eventually return matching IDs and hydrate current values from
PostgreSQL. The consumer is not implemented yet, so events remain pending and
products are not automatically searchable in Meilisearch at this step.

Stock adjustment uses one conditional database update. Arithmetic uses a wider
intermediate type so both underflow and integer overflow are rejected cleanly.
Metadata updates lock the product while comparing values and writing the outbox
event, protecting concurrent edits from lost synchronization intent.

Run the HTTP/database validation script:

```bash
docker compose exec -T app python < scripts/check_products.py
```

It checks CRUD validation, outbox counts, concurrent stock updates, and rollback
after injected outbox failures. Only its own products and outbox events are
removed. Browser Swagger rendering is not an automated test; the same documented
operations are exercised over real HTTP.

## Run locally

Requires Python 3.12, pip, and a reachable PostgreSQL instance. Run these commands
from the repository root, replacing the URL placeholders with your database:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export DATABASE_URL='postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DATABASE'
.venv/bin/python -m uvicorn app.main:app --reload
```

- Health: <http://localhost:8000/health> returns `{"status":"ok"}`.
- Swagger/OpenAPI interface: <http://localhost:8000/docs>.
- OpenAPI schema: <http://localhost:8000/openapi.json>.

Compose loads `.env` into the app's process environment. Local Python execution
does not automatically load it, and the Compose hostname `postgres` is not
resolvable outside its network. Use Docker Compose for the complete local setup.

The health endpoint is a liveness check; it does not recheck external dependencies.
Frontend UI is explicitly out of scope. Swagger provides manual API interaction.

## Structure

Routes in `app/api` handle HTTP behavior, with Pydantic models in `app/schemas`.
Business rules belong in `app/services`; persistence and search details
belong in `app/repositories`, `app/db`, and `app/search`. Shared configuration
belongs in `app/core`. These packages establish small boundaries without adding
a generic framework or unnecessary business layers to the health endpoint.

## Inventory repository boundary

`InventoryRepository` is a small Python protocol with `reserve_stock` and
`product_exists`. Its immutable `StockReservation` result contains the product
ID, remaining stock, and exact Decimal unit price. The PostgreSQL implementation
owns the conditional `UPDATE ... WHERE stock >= quantity RETURNING ...` query;
future services will call the contract without embedding this SQL.

Reservation requires an existing caller-owned transaction. The repository never
commits or rolls back, allowing the eventual order service to reserve multiple
items and save the order atomically. A failed reservation returns `None`;
`product_exists` distinguishes a missing product from insufficient stock.
Normal metadata persistence can use SQLAlchemy's portable operations, but moving
to MySQL or Oracle requires an adapted concurrency implementation and equivalent
validation. Only PostgreSQL is implemented for this assessment.

Check the repository against the running database:

```bash
docker compose exec -T app python < scripts/check_inventory_repository.py
```

Assertions cover reservation results, invalid quantities, missing products,
transaction ownership, rollback across multiple reservations, and two concurrent
transactions competing for one stock unit. The script creates and removes only
its own fixture products. This is database-level validation; HTTP order endpoints
and the full oversell load test are still pending.

See [Implementation Decisions](docs/IMPLEMENTATION_DECISIONS.md) for the
assessment, rationale, validation evidence, and remaining limitations.
