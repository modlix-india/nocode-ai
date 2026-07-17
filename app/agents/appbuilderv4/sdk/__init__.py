"""The `modlix` SDK exposed inside the code_run sandbox.

Designed to be imported as `import modlix` from a script the agent writes.
Loaded INSIDE the subprocess via the launcher (`runner.py`), which puts a
synthesised `modlix` module on `sys.modules` so the agent's script just
does:

    import modlix
    # GET an existing page to learn the shape
    page = modlix.pages.get("homeTwo", app_code="someApp")
    # build a new definition
    new_def = {...}
    # write it back
    modlix.pages.replace("home", new_def, app_code="clonelinear")

The SDK reads auth + gateway config from environment variables set by the
code_run tool just before the subprocess is spawned. Nothing is fetched
across requests — every call hits the gateway fresh.
"""

from app.agents.appbuilderv4.sdk import _components as components  # noqa: F401
from app.agents.appbuilderv4.sdk._core import (  # noqa: F401
    config,
    post,
    get,
    put,
    delete,
    catalog,
    pages,
    apps,
    uuid,
    _try_refresh_token as refresh_token,
)
from app.agents.appbuilderv4.sdk._validators import (  # noqa: F401
    ModlixShapeError,
    validate_page,
    validate_app_ui,
)
