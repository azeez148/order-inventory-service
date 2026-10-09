"""Reporting coordination through the repository contract."""
from app.repositories.reports import ReportingRepository, SalesSummaryRecord


class ReportingService:
    def __init__(self, repository: ReportingRepository) -> None:
        self._repository = repository

    async def sales_summary(self) -> SalesSummaryRecord:
        return await self._repository.sales_summary()
