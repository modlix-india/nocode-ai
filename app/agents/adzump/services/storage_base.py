import logging

from app.agents.appbuilder.tools._shared import get_saas_client
from app.agents.adzump.services.storage_models import (
    StorageRequest,
    StorageRequestWithPayload,
    StorageReadRequest,
    StorageUpdateWithPayload,
    StorageResponse,
)

logger = logging.getLogger(__name__)


class StorageService:
    """Base storage service that centralizes all NCLC Storage REST endpoints
    (Read, ReadPage, Create, Update).
    """

    READ = "/api/core/function/execute/CoreServices.Storage/Read"
    READ_PAGE = "/api/core/function/execute/CoreServices.Storage/ReadPage"
    CREATE = "/api/core/function/execute/CoreServices.Storage/Create"
    UPDATE = "/api/core/function/execute/CoreServices.Storage/Update"

    def __init__(self):
        self.client = get_saas_client()

    def _headers(self, app_code: str, auth_headers: dict = None) -> dict:
        h = dict(auth_headers) if auth_headers else {}
        h["AppCode"] = app_code
        h["Content-Type"] = "application/json"
        return h

    async def read_storage(self, request: StorageRequest, auth_headers: dict = None) -> StorageResponse:
        try:
            result = await self.client.post(
                self.READ,
                headers=self._headers(request.appCode, auth_headers),
                json=request.model_dump(),
            )
            return StorageResponse(
                success=result.success, result=result.data, error=result.error
            )
        except Exception as e:
            return StorageResponse(success=False, error=str(e))

    async def read_page_storage(self, request: StorageReadRequest, auth_headers: dict = None) -> StorageResponse:
        try:
            result = await self.client.post(
                self.READ_PAGE,
                headers=self._headers(request.appCode, auth_headers),
                json=request.model_dump(),
            )
            return StorageResponse(
                success=result.success, result=result.data, error=result.error
            )
        except Exception as e:
            return StorageResponse(success=False, error=str(e))

    async def write_storage(
        self, request: StorageRequestWithPayload, auth_headers: dict = None
    ) -> StorageResponse:
        try:
            result = await self.client.post(
                self.CREATE,
                headers=self._headers(request.appCode, auth_headers),
                json=request.model_dump(),
            )
            return StorageResponse(
                success=result.success, result=result.data, error=result.error
            )
        except Exception as e:
            return StorageResponse(success=False, error=str(e))

    async def update_storage(
        self, request: StorageUpdateWithPayload, auth_headers: dict = None
    ) -> StorageResponse:
        try:
            result = await self.client.post(
                self.UPDATE,
                headers=self._headers(request.appCode, auth_headers),
                json=request.model_dump(),
            )
            return StorageResponse(
                success=result.success, result=result.data, error=result.error
            )
        except Exception as e:
            return StorageResponse(success=False, error=str(e))


# Singleton instance for shared use
storage_service = StorageService()
