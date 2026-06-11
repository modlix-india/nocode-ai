import asyncio
import random
import logging
from typing import Callable, Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Singleton client instance shared across the agent's tasks
_client: Optional[httpx.AsyncClient] = None

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def get_http_client() -> httpx.AsyncClient:
    """Get or initialize the shared AsyncClient."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client


async def http_request(
    method: str,
    url: str,
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    error_handler: Optional[Callable[[httpx.Response], None]] = None,
    retry_delay_parser: Optional[Callable[[httpx.Response, float], float]] = None,
    **kwargs: Any,
) -> httpx.Response:
    """HTTP request with built-in retry (exponential backoff + jitter).
    
    Ported from Adzump-AI/core/infrastructure/http_client.py.
    """
    client = get_http_client()

    for attempt in range(max_attempts):
        try:
            response = await client.request(method, url, **kwargs)

            if response.is_success:
                return response

            is_last = (attempt == max_attempts - 1)
            # If not retryable or it's the last attempt, trigger error handler
            if response.status_code not in RETRYABLE_STATUS_CODES or is_last:
                if error_handler:
                    error_handler(response)
                response.raise_for_status()

            delay = _compute_delay(attempt, base_delay, response, retry_delay_parser)
            logger.warning(
                "http_retry status=%s attempt=%s retry_in=%s url=%s",
                response.status_code, attempt + 1, round(delay, 2), url
            )
            await asyncio.sleep(delay)

        except httpx.TimeoutException:
            if attempt == max_attempts - 1:
                raise
            delay = _compute_delay(attempt, base_delay)
            logger.warning(
                "http_timeout_retry attempt=%s retry_in=%s url=%s",
                attempt + 1, round(delay, 2), url
            )
            await asyncio.sleep(delay)

        except httpx.HTTPStatusError:
            # Re-raise if it's already a status error (already handled by error_handler)
            raise

    raise RuntimeError(f"Request to {url} failed after {max_attempts} attempts")


def _compute_delay(
    attempt: int,
    base_delay: float,
    response: Optional[httpx.Response] = None,
    retry_delay_parser: Optional[Callable[[httpx.Response, float], float]] = None,
) -> float:
    delay = base_delay * (2**attempt)
    if response and retry_delay_parser:
        delay = retry_delay_parser(response, delay)
    # Full jitter: random value in [0, delay] spreads retries across the full backoff window
    return random.uniform(0, delay)
