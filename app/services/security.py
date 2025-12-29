"""Security service integration for token validation"""
import httpx
import logging
from fastapi import HTTPException, Header, Request, Depends
from typing import Optional
from app.config import settings
from app.api.models.auth import ContextAuthentication

logger = logging.getLogger(__name__)


async def get_context_authentication(
    request: Request,
    authorization: Optional[str] = Header(None),
    client_code: Optional[str] = Header(None, alias="clientCode"),
    app_code: Optional[str] = Header(None, alias="appCode"),
) -> ContextAuthentication:
    """
    Validate token via security service, matching JWTTokenFilter behavior.
    
    Calls: GET /api/security/internal/securityContextAuthentication
    Headers: Authorization, X-Forwarded-Host, X-Forwarded-Port, clientCode, appCode
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    
    # Get forwarded headers (set by gateway)
    forwarded_host = request.headers.get("X-Forwarded-Host", request.url.hostname or "localhost")
    forwarded_port = request.headers.get("X-Forwarded-Port", str(request.url.port or 80))
    
    # Handle comma-separated port (like Java does)
    if "," in forwarded_port:
        forwarded_port = forwarded_port.split(",")[0]
    
    security_url = f"{settings.SECURITY_SERVICE_URL}/api/security/internal/securityContextAuthentication"
    request_headers = {
        "Authorization": authorization,
        "X-Forwarded-Host": forwarded_host,
        "X-Forwarded-Port": forwarded_port,
        "clientCode": client_code or "",
        "appCode": app_code or "",
    }
    
    logger.info(f"Calling security service: {security_url}")
    logger.debug(f"Security request headers: X-Forwarded-Host={forwarded_host}, X-Forwarded-Port={forwarded_port}, appCode={app_code}")
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(security_url, headers=request_headers)
            
            logger.info(f"Security service response: {response.status_code}")
            
            if response.status_code != 200:
                response_text = response.text
                logger.warning(f"Authentication failed: {response.status_code}, response: {response_text[:500]}")
                raise HTTPException(
                    status_code=401,
                    detail=f"Authentication failed: {response.status_code} - {response_text[:200]}"
                )
            
            auth_data = response.json()
            logger.info(f"Auth response data: {auth_data}")
            logger.info(f"Auth successful: isAuthenticated={auth_data.get('isAuthenticated')}, appCode={auth_data.get('verifiedAppCode') or auth_data.get('urlAppCode')}")
            return ContextAuthentication(**auth_data)
            
        except httpx.RequestError as e:
            logger.error(f"Security service unavailable at {security_url}: {e}")
            raise HTTPException(
                status_code=503,
                detail=f"Security service unavailable at {settings.SECURITY_SERVICE_URL}: {str(e)}"
            )


async def require_auth(
    auth: ContextAuthentication = Depends(get_context_authentication)
) -> ContextAuthentication:
    """Dependency for protected routes - ensures user is authenticated"""
    if not auth.isAuthenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return auth


def require_authority(authority: str):
    """Factory for authority-checking dependency"""
    async def check_authority(
        auth: ContextAuthentication = Depends(require_auth)
    ) -> ContextAuthentication:
        if auth.user and auth.user.stringAuthorities:
            if authority not in auth.user.stringAuthorities:
                raise HTTPException(
                    status_code=403,
                    detail=f"Missing authority: {authority}"
                )
        return auth
    return check_authority


# Allowed apps for AI features
ALLOWED_AI_APPS = {"sitezump", "appbuilder"}


async def require_ai_access(
    auth: ContextAuthentication = Depends(require_auth)
) -> ContextAuthentication:
    """
    Dependency for AI routes - ensures user has access to sitezump or appbuilder.
    
    The security service validates app access and returns verifiedAppCode.
    We check if the verified app is in the allowed list.
    """
    # Check verifiedAppCode first (verified by security service)
    app_code = auth.verifiedAppCode or auth.urlAppCode
    
    if not app_code:
        raise HTTPException(
            status_code=403,
            detail="AI features require sitezump or appbuilder access. No app code provided."
        )
    
    # Check if the app is in the allowed list
    if app_code.lower() not in ALLOWED_AI_APPS:
        logger.warning(f"AI access denied for app: {app_code}")
        raise HTTPException(
            status_code=403,
            detail=f"AI features are only available in sitezump or appbuilder applications."
        )
    
    logger.info(f"AI access granted for app: {app_code}, user: {auth.user.userName if auth.user else 'unknown'}")
    return auth

