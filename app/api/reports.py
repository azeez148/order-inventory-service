"""Reporting routes explicitly use the isolated reporting dependency."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_reporting_session
from app.repositories.postgres_reports import PostgreSQLReportingRepository
from app.schemas.reports import SalesSummaryResponse
from app.services.reports import ReportingService

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


async def get_reporting_service(
    session: Annotated[AsyncSession, Depends(get_reporting_session)],
) -> ReportingService:
    return ReportingService(PostgreSQLReportingRepository(session))


@router.get(
    "/sales-summary", response_model=SalesSummaryResponse,
    responses={503: {"description": "Reporting connection capacity is exhausted"},
               504: {"description": "Reporting statement timed out"}},
)
async def sales_summary(
    service: Annotated[ReportingService, Depends(get_reporting_service)],
) -> SalesSummaryResponse:
    return SalesSummaryResponse.model_validate(await service.sales_summary())
