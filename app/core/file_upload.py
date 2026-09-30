"""Put raw bytes into the Modlix files service.

Everything else in this service that uploads a file takes a LOCAL PATH and
posts it as multipart (`app/agents/appbuilder/tools/modlix/visuals.py`), which
is right for the tools: an agent names a file it has already written. Chat
attachments arrive as base64 in a request body and never touch the disk, so
they need a bytes-first path, and they need one thing the multipart route
cannot give them.

**Why the internal route rather than the public one.** `POST /api/files/{store}`
has no lifetime parameter at all. `POST /api/files/internal/{store}` does
(`expiresAfterMinutes`), and a lifetime is the entire point of storing a chat
attachment: it is what lets the existing `FILES_TTL_CLEANUP` worker collect it
in ninety days without a sweep that has to work out for itself which files it is
allowed to touch. The internal route is also what `message/BridgeMediaService`
uses for WhatsApp media, which is the same problem with a different number.

Two consequences of using it:

* The body is the raw bytes, not a multipart envelope, and `fileName` is an
  explicit query parameter. The controller reads `getContentLengthLong()`, so
  the request must carry a Content-Length -- passing `content=bytes` to httpx
  does that; a generator would not.
* `/api/files/internal/**` is permitAll inside the cluster (nginx blocks it at
  the edge). **So `client_code` must come from the verified AuthContext and
  never from anything the caller sent.** There is no second check behind this.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Read access to a secured file is decided by
# `SecuredFileResourceService.checkReadAccessWithClientCode`. It consults
# `files_access_path` for ordinary paths -- which means a path under a new
# prefix is 403 for everyone until someone seeds rows for it -- and
# short-circuits for four special first folders. `_withInClient` is the one that
# means "any authenticated user of this client", which is exactly the audience
# for a chat attachment, and it needs no rows seeded.
#
# WhatsApp's `/whatsapp/{app}/...` paths get away without this only because the
# bridge reads them back server-side through the internal endpoint. Ours are
# fetched by a browser, so they must sit here.
SECURED_ROOT = "_withInClient"

# Our own namespace under that root, so we are not sharing a directory with
# whatever else a tenant keeps in it.
CHAT_FOLDER = "aichat"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

# Extensions worth guessing when a client sends a mime type and a useless name.
_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/csv": "csv",
    "application/json": "json",
}


@dataclass
class UploadedFile:
    """What the files service gives back that is worth keeping.

    The same four fields WhatsApp keeps out of its `FileDetail`, and for the
    same reason: `file_path` is the only handle anything has for deleting the
    file later, and `url` is the only one a browser can fetch.
    """

    name: str
    file_path: str
    url: str
    size_bytes: int


def sanitise_file_name(name: str, mime_type: str | None = None) -> str:
    """A file name safe to put in a URL path, with a uuid to make it unique.

    Unique rather than merely safe, because two pastes in one turn are both
    called `image.png`, and `createFileFromInputStream` answers a name clash
    without `override` by returning the EXISTING file and a 200 -- so a
    collision would not fail, it would silently hand back the wrong image.
    """
    base = _SAFE_NAME.sub("_", (name or "").strip()).strip("._-")
    if not base or base in {"undefined", "null", "NaN"}:
        ext = _MIME_EXT.get((mime_type or "").split(";", 1)[0].strip().lower(), "bin")
        base = f"attachment.{ext}"
    # Long enough to be recognisable, short enough to keep FILE_PATH inside the
    # 512-character column that carries the unique key on ai_session_attachment.
    if len(base) > 100:
        stem, dot, ext = base.rpartition(".")
        base = f"{stem[:90]}{dot}{ext}" if dot else base[:100]
    return f"{uuid.uuid4().hex[:8]}-{base}"


def chat_attachment_dir(access_app_code: str, session_id: str, turn_number: int) -> str:
    """Where one turn's attachments live, client-relative.

    `access_app_code` (appbuilder / sitezump) rather than the app being built:
    the file belongs to the chat, and `app_code` is routinely empty on a session
    that has not created an app yet.
    """
    app = _SAFE_NAME.sub("_", access_app_code or "appbuilder")
    sid = _SAFE_NAME.sub("_", session_id or "unknown")
    return f"/{SECURED_ROOT}/{CHAT_FOLDER}/{app}/{sid}/t{turn_number}"


async def upload_bytes(
    data: bytes,
    *,
    store: str,
    client_code: str,
    file_path: str,
    file_name: str,
    expires_after_minutes: int | None = None,
    override: bool = False,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> UploadedFile | None:
    """Write `data` into the files service. None on any failure.

    Deliberately never raises. Every caller is storing something alongside work
    the user is already waiting on, and losing the copy is worth strictly less
    than losing the turn.
    """
    if not data:
        return None

    params: dict[str, str] = {
        "clientCode": client_code,
        "override": "true" if override else "false",
        "filePath": file_path,
        "fileName": file_name,
    }
    if expires_after_minutes is not None:
        params["expiresAfterMinutes"] = str(expires_after_minutes)

    request_headers = dict(headers or {})
    request_headers["Content-Type"] = "application/octet-stream"

    url = f"{settings.GATEWAY_URL}/api/files/internal/{store}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url, params=params, content=data, headers=request_headers
            )
    except Exception as e:  # noqa: BLE001 — see the docstring
        logger.warning("file_upload failed for %s/%s: %s", file_path, file_name, e)
        return None

    if response.status_code != 200:
        logger.warning(
            "file_upload rejected for %s/%s: HTTP %s %s",
            file_path, file_name, response.status_code, response.text[:200],
        )
        return None

    try:
        detail = response.json()
    except Exception:  # noqa: BLE001
        logger.warning("file_upload returned non-JSON for %s/%s", file_path, file_name)
        return None

    stored_path = detail.get("filePath") or f"{file_path}/{file_name}"
    stored_url = detail.get("url") or ""
    # The files service returns the url without a leading slash
    # ("api/files/secured/file/..."). A bare relative path resolves against
    # whatever deep page route the chat happens to be on and 404s; root-relative
    # works everywhere the browser might be.
    if stored_url and not stored_url.startswith(("http://", "https://", "/")):
        stored_url = f"/{stored_url}"

    return UploadedFile(
        name=detail.get("name") or file_name,
        file_path=stored_path,
        url=stored_url,
        size_bytes=int(detail.get("size") or len(data)),
    )
