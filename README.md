# Order & Inventory Service

An asynchronous FastAPI backend for a shared order and inventory service.
The current implementation includes the FastAPI scaffold and a Docker development
environment with PostgreSQL and Meilisearch, plus the initial database schema.
The app connects through a bounded asynchronous SQLAlchemy pool and supports
product creation, retrieval, metadata updates, and atomic stock adjustments.
Transactional orders and product search with authoritative database hydration
and automatic outbox synchronization are implemented. Reporting has separate
connection capacity; its aggregate API will follow incrementally.

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
health/docs, product/inventory endpoints, and transactional order endpoints.
The product search route uses Meilisearch for matching and PostgreSQL for current
product values. A lifespan-managed worker automatically processes pending outbox
events and retries search failures.
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
automatically commit. Shutdown cancels/awaits the outbox worker, closes its HTTP
client, then disposes the engine. An `AsyncSession` belongs to
one request/task and must not be shared among concurrent tasks.

The active outbox consumer uses one connection from this OLTP pool while awaiting
indexing. It locks its event, not product rows; warehouse/order writes can continue.
This is an accepted baseline trade-off to evaluate in the mixed workload tests.

Pools are per process: each worker can open up to ten OLTP plus two reporting
connections, so four workers can allow 48 total before other clients. PgBouncer can provide centralized
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

## Reporting connection isolation

Reporting uses its own async engine and session factory, with a smaller pool on
the same PostgreSQL instance. Future report routes must use
`get_reporting_session`; product, order, search hydration, and the outbox continue
using the OLTP pool. Reporting startup connectivity is checked, and both engines
are disposed on shutdown or startup failure.

| Variable | Default | Meaning |
|---|---|---|
| `REPORTING_POOL_SIZE` | `2` | Maximum report connections, with overflow fixed at zero |
| `REPORTING_POOL_TIMEOUT` | `5` seconds | Wait limit for reporting connection acquisition |
| `REPORTING_STATEMENT_TIMEOUT_MS` | `10000` | PostgreSQL timeout for each reporting statement |

Settings reject nonpositive limits and require reporting capacity to be smaller
than total OLTP capacity. Two reporting connections constrain heavy aggregates
without taking away the ten-connection OLTP budget. The ten-second statement
limit is a local starting point to test against seeded data, not a performance
claim. It applies to each statement, not the complete request.

asyncpg supplies `statement_timeout` and `default_transaction_read_only=on` when
opening reporting connections. These settings stay on that pool's connections
and do not leak into OLTP. Read-only defaults prevent accidental writes in report
code; production could also use a dedicated database role with read-only grants.
Separate pools isolate connection capacity, while PostgreSQL CPU, memory, and
I/O remain shared. Read replicas or materialized views are future options.
Transaction-pooling PgBouncer would require separate validation of these settings.

```bash
docker compose exec -T app python -m scripts.check_reporting_pool
```

The check saturates reporting capacity while verifying OLTP connectivity,
rejects accidental writes, cancels a real aggregate using a short test timeout,
and verifies connection reuse/settings/lifecycle cleanup. No report API exists
yet; Step 14 will add the sales summary and its HTTP timeout behavior.

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
search returns matching IDs and hydrates current values from
PostgreSQL. The worker indexes committed product state asynchronously; new
products and text changes become searchable after their events are processed.

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

## Orders and concurrent inventory

`POST /api/v1/orders` accepts an object containing item lines:

```json
{"items":[{"product_id":1,"quantity":2},{"product_id":1,"quantity":3}]}
```

Replace the example ID with a product you created. The service combines duplicate
product IDs, so this request reserves five units and stores one item line. It
reserves products in ascending ID order to reduce deadlock risk for overlapping
multi-product orders. The response is 201 with status `created`, Decimal total,
timestamp, and stored item quantities/unit prices. Retrieve it through
`GET /api/v1/orders/{id}`. Later product-price changes leave historical order
prices and totals unchanged. Clients do not supply order prices or totals.

Each reservation uses the inventory repository's conditional atomic update:
`UPDATE products SET stock = stock - quantity WHERE stock >= quantity RETURNING ...`.
PostgreSQL checks and decrements together, including when concurrent transactions
compete for the same stock. A separate unlocked SELECT/check/UPDATE would allow
multiple callers to observe the same available units and oversell them. Python
locks would only coordinate one process; the database operation works across
workers accessing the same PostgreSQL database. The nonnegative-stock constraint
adds a final integrity check.

All reservations, order insertion, and item insertion happen in one transaction.
A missing product returns 404; insufficient stock returns 409. Either failure
rolls back earlier reservations and leaves no partial order. Invalid input returns
422, an unknown order returns 404, and pool acquisition timeout returns 503.
Requests allow 1–100 lines, positive integer IDs/quantities, and combined quantities
within PostgreSQL's integer range. A total exceeding `NUMERIC(18,2)` is rejected
with 422 and complete rollback. Monetary values remain exact decimals.

Run the repeatable HTTP/database checks:

```bash
docker compose exec -T app python < scripts/check_orders.py
```

Checks include duplicate normalization, rollback for missing/insufficient items,
price history, input/total bounds, injected order persistence failure, reversed
multi-product requests, and two concurrent orders against stock one. The observed
result was exactly one 201, one 409, and final stock zero. This is a correctness
check, not a capacity benchmark; the full oversell/mixed-workload scripts are
still future steps. Only script-owned fixture data is removed.

Repeated requests create separate orders; idempotency, cancellation, and stock
restoration workflows are not implemented. A lost response after commit therefore
requires careful client handling rather than automatic retry.

## Search provider architecture

`SearchProvider` defines product text search, indexing, and removal. Its
`SearchProduct` projection contains only ID, name, and description. The implemented
`MeilisearchProvider` uses one lifespan-managed `httpx.AsyncClient` with bounded
HTTP connections. No blocking SDK or SQL wildcard text search is used.

| Variable | Default | Meaning |
|---|---|---|
| `SEARCH_PROVIDER` | `meilisearch` | Only implemented provider; other values fail validation |
| `MEILISEARCH_URL` | `http://meilisearch:7700` | Internal search service URL |
| `MEILISEARCH_MASTER_KEY` | Empty | Bearer key; Compose supplies the local development key |
| `MEILISEARCH_INDEX` | `products` | Index UID with letters, digits, underscores, or hyphens |
| `SEARCH_HTTP_TIMEOUT` | `5` seconds | HTTP network timeout |
| `SEARCH_TASK_TIMEOUT` | `30` seconds | Maximum polling time for each indexing task |

On first use, the adapter creates or checks the index's `id` primary key and sets
searchable attributes to name then description. Setup is guarded within each
process and retried after failure. If an initialized index is lost, a 404 resets
setup so a subsequent call can recreate it. Searches return only ranked IDs;
the API hydrates current prices and stock from PostgreSQL.

Indexing/removal waits for the asynchronous task to reach `succeeded`. Failed,
canceled, malformed, or timed-out operations raise `SearchProviderError` with a
sanitized message. A timeout does not cancel server-side work: idempotent upserts
allow the outbox consumer to retry uncertain completion safely. The client
is closed on lifespan exit. Search connection/index setup is lazy, so application
lifespan itself does not require a search network call; Compose still waits for
service health during stack startup.

Meilisearch fits the assessment's external indexing requirement with a small
Docker service, ranking, and typo tolerance. Elasticsearch/OpenSearch add more
operations than needed here. PostgreSQL full-text search is a valid alternative,
but wildcard `ILIKE` would put primary text-search work on the transactional
database. The contract allows future provider replacement while acknowledging
that adapters and their behavior need their own implementation and tests.

```bash
docker compose exec -T app python < scripts/check_search_provider.py
```

The script checks failure handling with HTTPX's mock transport, validates behavior
against a separate randomly named live index, and removes only that index.
Product outbox events are consumed automatically by the worker described below.
See [Meilisearch's task lifecycle reference](https://www.meilisearch.com/docs/reference/api/async-task-management/get-task)
for why queued writes need completion checks.

## Product search API

`GET /api/v1/products/search?q=football&limit=20` returns a JSON array of current
product records in search relevance order. `q` is required, trimmed, and must
contain 1–200 characters. `limit` defaults to 20 and allows 1–100. Invalid query
parameters return 422; no matches return 200 with `[]`.

The service asks `SearchProvider` for ranked IDs, fetches matching PostgreSQL
products with one batched query, and restores their relevance order. It skips
database access for empty matches. Stale index IDs whose products are absent
are omitted, so results can contain fewer records than the requested limit.
Returned name, description, price, stock, and timestamps are all current database
values as of the read; search results do not reserve inventory.

An outdated text projection can still match old wording until synchronization
finishes, but its response never trusts indexed transactional values. Search
failures return a generic 503 rather than silently switching to SQL text matching.
The search network call completes before hydration checks out a database
connection.

```bash
docker compose exec -T app python < scripts/check_search_api.py
```

This script creates fixture products via HTTP, explicitly indexes them through
the provider, exercises the real API, and removes only its own data/documents.
Checks cover ranking, one-query hydration, stale product/text projections,
current prices/stock, validation, and sanitized provider failure handling.
Controlled provider snapshots simulate stale matches deterministically while the
real worker is active; automatic synchronization is tested separately below.

## Transactional search synchronization

Product writes and `upsert` events commit together. A background asyncio task
polls the durable PostgreSQL outbox during FastAPI lifespan. It claims pending
events using `FOR UPDATE SKIP LOCKED`, reads current product text, awaits the
provider's confirmed indexing success, and sets `processed_at` before committing.
The acknowledgement uses actual clock time rather than transaction-start time.

An advisory transaction lock permits one active consumer per outbox table across
workers/replicas, preventing overlapping old/new projections. The lock uses a
reserved negative table-OID key and releases automatically on commit, rollback,
or connection loss. This intentionally favors simple ordering over parallel
indexing throughput. Product rows are read without locks during the search call.

| Variable | Default | Meaning |
|---|---|---|
| `OUTBOX_ENABLED` | `true` | Start the lifespan worker; `false` disables it |
| `OUTBOX_POLL_INTERVAL` | `1` second | Delay after no work or a failed attempt |

If search fails, the transaction rolls back and the event remains pending. The
worker logs the error class without private payloads, sleeps, and retries.
Events survive process restarts. If indexing succeeds before acknowledgement
fails, retry resends current state using an idempotent upsert. This is at-least-once
delivery, not a distributed transaction or an exactly-once guarantee. A later
product edit creates another event, ensuring eventual convergence to current
committed text. Orders and product mutations do not call Meilisearch directly.

Shutdown cancels and awaits the worker before closing the HTTP client/engine.
Cancellation rolls back active acknowledgement work, leaving durable intent
retryable. Processed rows are retained for this assessment. An unsupported or
persistently failing oldest event can block the queue; dead-letter handling,
backoff, retention, and dedicated workers are future operational improvements.
Index loss still requires rebuilding already processed projections; recreating
the empty index alone is insufficient.

```bash
docker compose exec -T app python -m scripts.check_outbox
```

The check verifies automatic HTTP create/update synchronization and processed
timestamps against the running app. A disposable PostgreSQL schema separately
tests network failure/recovery, competing consumers, nonblocking product writes,
acknowledgement failure, skipped locks, and normal/exceptional shutdown. Only
test-owned schema/data/documents are removed. Validation scripts are included in
the app image, so they can also run as `python -m scripts.<name>`.

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
its own fixture products. This is database-level validation; use the order script
above for HTTP transaction checks. The full oversell load test is still pending.

See [Implementation Decisions](docs/IMPLEMENTATION_DECISIONS.md) for the
assessment, rationale, validation evidence, and remaining limitations.
