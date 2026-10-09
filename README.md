# Order & Inventory Service

An asynchronous FastAPI backend for a shared order and inventory service.
The current implementation is the Step 1 application scaffold; database,
product, order, search, and reporting functionality will follow incrementally.

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
