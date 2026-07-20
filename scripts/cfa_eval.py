"""CFA app evaluation — four independent gates per app.

Run after `cfa_drive.py run <scenario>` to score whether the build actually
produced something a user can use:

  python scripts/cfa_eval.py <app_code>
  python scripts/cfa_eval.py clonelinear
  python scripts/cfa_eval.py taskmate

Gates:
  1. STRUCTURAL — validate_page on every page returns success (no shape
     violations: properties wrapped right, bindingPath shape valid,
     children resolve, event-fn refs resolve).
  2. INVENTORY — at least one page exists; every page has ≥2 components
     (root + at least one child). A page that's "just root" is an empty
     shell from an agent that quit early.
  3. RENDER — drive_page anonymously on every page (and on /home if it
     requires auth, expect the login wrapper to substitute). For each
     rendered page: zero console pageerrors AND visible content above a
     non-blank threshold (≥ 50 chars of body text OR ≥1 input/button).
  4. VISUAL — for scenarios that specify a `clone_target.url`, screenshot
     the source URL and the built page, ask Claude to diff them; PASS when
     there are zero `severity=high` diffs. Skipped (n/a) when the scenario
     has no `clone_target` block.

Output: JSON scorecard to stdout AND to
  scripts/cfa_runs/_evals/<app_code>/<run-ts>/scorecard.json

Exit code: 0 if all required gates pass; 1 otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

import httpx


REPO_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = REPO_ROOT / "scripts" / "cfa_runs" / "_evals"
SCENARIOS_DIR = REPO_ROOT / "scripts" / "cfa_scenarios"
JWT_PATH = Path.home() / ".cfa-jwt"
CREDS_PATH = Path.home() / ".cfa-creds"

SAAS_BASE_URL = "http://localhost:8080"
APPS_BASE_URL = "https://apps.local.modlix.com"
FORWARDED_HOST = "localhost:8080"
FORWARDED_PORT = "8080"

# Render thresholds — tuned for "did anything composable land on screen".
MIN_BODY_TEXT_CHARS = 50
MIN_INTERACTIVE_ELEMENTS = 1
MIN_COMPONENTS_PER_PAGE = 2  # root + at least one child


def _read_creds() -> dict[str, str] | None:
    if not CREDS_PATH.exists():
        return None
    return json.loads(CREDS_PATH.read_text())


async def _login_fresh(creds: dict[str, str]) -> str:
    body = {
        "userName": creds["username"],
        "password": creds["password"],
        "identifierType": creds.get("identifierType", "EMAIL_ID"),
        "loggedInClientCode": creds.get("clientCode", "SYSTEM"),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Forwarded-Host": FORWARDED_HOST,
        "X-Forwarded-Port": FORWARDED_PORT,
        "clientCode": creds.get("clientCode", "SYSTEM"),
        "appCode": creds.get("appCode", "appbuilder"),
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{SAAS_BASE_URL}/api/security/authenticate", json=body, headers=headers)
    r.raise_for_status()
    token = r.json()["accessToken"]
    JWT_PATH.write_text(token)
    JWT_PATH.chmod(0o600)
    return token


async def _ensure_jwt() -> str:
    if JWT_PATH.exists():
        return JWT_PATH.read_text().strip()
    creds = _read_creds()
    if not creds:
        raise SystemExit("No JWT at ~/.cfa-jwt and no creds at ~/.cfa-creds")
    return await _login_fresh(creds)


def _auth_headers(jwt: str, client_code: str, app_code: str = "appbuilder") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {jwt}",
        "clientCode": client_code,
        "appCode": app_code,
        "X-Forwarded-Host": FORWARDED_HOST,
        "X-Forwarded-Port": FORWARDED_PORT,
    }


# ── Gate 1: STRUCTURAL ───────────────────────────────────────────────────


async def gate_structural(
    jwt: str, app_code: str, client_code: str
) -> dict[str, Any]:
    """Call validate_page on every page; record per-page pass/fail + violation list."""
    sys.path.insert(0, str(REPO_ROOT))
    from app.agents.appbuilder.tools.modlix.pages import (
        _execute_validate_page,
        _execute_list_pages,
    )

    ctx = {
        "app_code": app_code, "client_code": client_code,
        "headers": _auth_headers(jwt, client_code, app_code),
    }
    pages_result = await _execute_list_pages({"app_code": app_code}, ctx)
    if not pages_result.success:
        return {"gate": "structural", "ok": False, "reason": f"list_pages failed: {pages_result.error}"}

    page_names = _extract_page_names(pages_result.summary or "")
    if not page_names:
        return {"gate": "structural", "ok": False, "reason": "no pages found", "pages": []}

    per_page: list[dict[str, Any]] = []
    overall_ok = True
    for name in page_names:
        vr = await _execute_validate_page({"name": name, "app_code": app_code}, ctx)
        per_page.append({
            "name": name,
            "ok": vr.success,
            "detail": (vr.summary if vr.success else vr.error)[:600],
        })
        if not vr.success:
            overall_ok = False

    return {"gate": "structural", "ok": overall_ok, "pages": per_page}


def _extract_page_names(list_pages_summary: str) -> list[str]:
    """Pull page names from list_pages' JSON-in-string summary."""
    start = list_pages_summary.find("[")
    end = list_pages_summary.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        arr = json.loads(list_pages_summary[start : end + 1])
        return [p["name"] for p in arr if isinstance(p, dict) and p.get("name")]
    except (ValueError, KeyError):
        return []


# ── Gate 2: INVENTORY ────────────────────────────────────────────────────


async def gate_inventory(
    jwt: str, app_code: str, client_code: str
) -> dict[str, Any]:
    """Every page must have at least MIN_COMPONENTS_PER_PAGE components."""
    sys.path.insert(0, str(REPO_ROOT))
    from app.agents.appbuilder.tools.modlix.pages import (
        _execute_get_page,
        _execute_list_pages,
    )

    ctx = {
        "app_code": app_code, "client_code": client_code,
        "headers": _auth_headers(jwt, client_code, app_code),
    }
    pages_result = await _execute_list_pages({"app_code": app_code}, ctx)
    if not pages_result.success:
        return {"gate": "inventory", "ok": False, "reason": "list_pages failed"}

    page_names = _extract_page_names(pages_result.summary or "")
    if not page_names:
        return {"gate": "inventory", "ok": False, "reason": "no pages found"}

    per_page: list[dict[str, Any]] = []
    overall_ok = True
    for name in page_names:
        gp = await _execute_get_page({"name": name, "include": "tree", "app_code": app_code}, ctx)
        # The tree text contains "N components" — parse it.
        count = _parse_component_count(gp.summary or "")
        page_ok = count >= MIN_COMPONENTS_PER_PAGE
        per_page.append({"name": name, "components": count, "ok": page_ok})
        if not page_ok:
            overall_ok = False

    return {"gate": "inventory", "ok": overall_ok, "pages": per_page,
            "min_components_per_page": MIN_COMPONENTS_PER_PAGE}


def _parse_component_count(tree_summary: str) -> int:
    """`get_page` tree summary looks like 'Page X (N components, M event fns):' — pull N."""
    import re
    m = re.search(r"\((\d+)\s+components?", tree_summary)
    return int(m.group(1)) if m else 0


# ── Gate 3: RENDER ───────────────────────────────────────────────────────


async def gate_render(
    app_code: str, client_code: str
) -> dict[str, Any]:
    """Drive every page anonymously via Playwright; assert non-blank + zero pageerrors."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"gate": "render", "ok": False, "reason": "playwright not installed"}

    jwt = await _ensure_jwt()
    sys.path.insert(0, str(REPO_ROOT))
    from app.agents.appbuilder.tools.modlix.pages import _execute_list_pages
    ctx = {
        "app_code": app_code, "client_code": client_code,
        "headers": _auth_headers(jwt, client_code, app_code),
    }
    pages_result = await _execute_list_pages({"app_code": app_code}, ctx)
    if not pages_result.success:
        return {"gate": "render", "ok": False, "reason": "list_pages failed"}

    page_names = _extract_page_names(pages_result.summary or "")
    if not page_names:
        return {"gate": "render", "ok": False, "reason": "no pages"}

    per_page: list[dict[str, Any]] = []
    overall_ok = True

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for name in page_names:
            url = f"{APPS_BASE_URL}/{app_code}/{client_code}/page/{name}"
            ctx_browser = await browser.new_context(
                viewport={"width": 1440, "height": 900}, ignore_https_errors=True,
            )
            page = await ctx_browser.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)
            title = await page.title()
            body_text_len = await page.evaluate("document.body.innerText.length")
            inputs = await page.locator("input").count()
            buttons = await page.locator("button").count()
            screenshot_b64 = base64.b64encode(await page.screenshot(full_page=False, type="png")).decode("ascii")
            await ctx_browser.close()

            has_content = (body_text_len >= MIN_BODY_TEXT_CHARS
                           or (inputs + buttons) >= MIN_INTERACTIVE_ELEMENTS)
            no_errors = len(page_errors) == 0
            page_ok = has_content and no_errors
            if not page_ok:
                overall_ok = False

            per_page.append({
                "name": name,
                "url": url,
                "title": title,
                "body_text_chars": body_text_len,
                "input_count": inputs,
                "button_count": buttons,
                "page_errors": page_errors[:5],
                "ok": page_ok,
                "screenshot_b64_len": len(screenshot_b64),
                # screenshot itself written to disk by caller — pass through.
                "screenshot_b64": screenshot_b64,
            })

        await browser.close()

    return {"gate": "render", "ok": overall_ok, "pages": per_page,
            "thresholds": {"min_body_text_chars": MIN_BODY_TEXT_CHARS,
                           "min_interactive_elements": MIN_INTERACTIVE_ELEMENTS}}


# ── Gate 4: VISUAL (clone scenarios only) ───────────────────────────────


def _load_scenario(app_code: str) -> dict[str, Any] | None:
    """Locate a scenario YAML by `app_code`. Tries exact filename match first
    (`<app_code>.yaml`), then scans every YAML in the scenarios dir for a
    matching `app_code:` field. Scenario filenames use underscores
    (`clone_linear.yaml`) while app codes are alphabet-only (`clonelinear`),
    so the scan is the common case."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return None
    direct = SCENARIOS_DIR / f"{app_code}.yaml"
    if direct.exists():
        try:
            return yaml.safe_load(direct.read_text())
        except Exception:  # noqa: BLE001
            return None
    for path in SCENARIOS_DIR.glob("*.yaml"):
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and data.get("app_code") == app_code:
            return data
    return None


async def _shoot_external(url: str, scroll_fraction: float, viewport_width: int,
                          height: int = 900, wait_ms: int = 3000) -> tuple[bytes | None, str | None]:
    """Capture one full-viewport screenshot of an external URL at a scroll position."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, "playwright not installed"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(
                viewport={"width": viewport_width, "height": height},
                ignore_https_errors=True,
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(wait_ms)
            doc_h = await page.evaluate("document.documentElement.scrollHeight")
            y = int(max(0.0, min(1.0, scroll_fraction)) * max(0, doc_h - height))
            await page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
            await page.wait_for_timeout(400)
            png = await page.screenshot(full_page=False, type="png")
            await browser.close()
            return png, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


async def _diff_via_claude(src_b64: str, build_b64: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Send (source, build) pair to Claude and parse the structured-diff JSON."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from app.config import settings
        from app.agents.appbuilder.tools.modlix.clone_ops import _COMPARE_PROMPT, _safe_parse_json_array
        import anthropic  # type: ignore[import-not-found]
    except Exception as e:  # noqa: BLE001
        return None, f"import error: {e}"
    if not getattr(settings, "ANTHROPIC_API_KEY", ""):
        return None, "ANTHROPIC_API_KEY not set in settings"
    model = getattr(settings, "CLAUDE_SONNET", "claude-sonnet-4-6")
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        msg = await asyncio.to_thread(
            client.messages.create,
            model=model,
            max_tokens=4096,
            system="You produce strict JSON diff arrays for site-clone QA. Reply with ONLY the JSON array.",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "SOURCE (target to clone):"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": src_b64}},
                    {"type": "text", "text": "MODLIX BUILD (current state):"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": build_b64}},
                    {"type": "text", "text": _COMPARE_PROMPT},
                ],
            }],
        )
    except Exception as e:  # noqa: BLE001
        return None, f"Anthropic API error: {type(e).__name__}: {e}"
    raw = ""
    for block in (msg.content or []):
        if getattr(block, "type", "") == "text":
            raw += getattr(block, "text", "")
    return _safe_parse_json_array(raw)


async def gate_visual(
    app_code: str, client_code: str, scenario: dict[str, Any] | None, outdir: Path
) -> dict[str, Any]:
    """Visual fidelity gate for clone scenarios. Skipped when no clone_target is configured."""
    if not scenario or not isinstance(scenario.get("clone_target"), dict):
        return {"gate": "visual", "ok": True, "status": "n/a",
                "reason": "scenario has no clone_target — gate skipped"}

    target = scenario["clone_target"]
    source_url = (target.get("url") or "").strip()
    if not source_url.startswith(("http://", "https://")):
        return {"gate": "visual", "ok": False,
                "reason": "clone_target.url must be an absolute http(s) URL"}

    page_name = (target.get("page_name") or "home").strip() or "home"
    scroll_positions = target.get("scroll_positions") or [0.0, 0.5, 1.0]
    viewport_width = int(target.get("viewport_width") or 1440)
    severity_budget = target.get("severity_budget") or {"high": 0, "medium": 5}

    # Render source + build at each scroll position; diff each pair.
    per_shot: list[dict[str, Any]] = []
    overall_ok = True
    aggregate_counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0}

    from app.config import settings  # for PREVIEW_HOST
    sys.path.insert(0, str(REPO_ROOT))
    preview_host = (getattr(settings, "PREVIEW_HOST", "") or APPS_BASE_URL).rstrip("/")
    build_url = f"{preview_host}/{app_code}/{client_code}/page/{page_name}"

    for pos in scroll_positions:
        pos_f = float(pos)
        src_png, src_err = await _shoot_external(source_url, pos_f, viewport_width)
        if src_err or src_png is None:
            per_shot.append({"scroll": pos_f, "ok": False, "reason": f"source screenshot failed: {src_err}"})
            overall_ok = False
            continue
        # Build screenshot — same scroll fraction against the Modlix preview.
        build_png, build_err = await _shoot_external(build_url, pos_f, viewport_width)
        if build_err or build_png is None:
            per_shot.append({"scroll": pos_f, "ok": False, "reason": f"build screenshot failed: {build_err}"})
            overall_ok = False
            continue

        # Persist both shots for the run log.
        (outdir / f"visual_src_y{int(pos_f * 100):03d}.png").write_bytes(src_png)
        (outdir / f"visual_build_y{int(pos_f * 100):03d}.png").write_bytes(build_png)

        diffs, parse_err = await _diff_via_claude(
            base64.b64encode(src_png).decode("ascii"),
            base64.b64encode(build_png).decode("ascii"),
        )
        if diffs is None:
            per_shot.append({"scroll": pos_f, "ok": False, "reason": f"diff JSON unparsable: {parse_err}"})
            overall_ok = False
            continue

        sev_counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
        for d in diffs:
            sev = str(d.get("severity", "")).lower()
            if sev in sev_counts:
                sev_counts[sev] += 1
        for k in ("high", "medium", "low"):
            aggregate_counts[k] += sev_counts[k]

        # Budget enforcement is overall, not per-shot — applied after the loop.
        per_shot.append({
            "scroll": pos_f,
            "ok": True,
            "severity_counts": sev_counts,
            "diff_count": len(diffs),
            "top_diffs": diffs[:5],
        })

    if overall_ok:
        # Budget check across all shots.
        if aggregate_counts["high"] > int(severity_budget.get("high", 0)):
            overall_ok = False
        if aggregate_counts["medium"] > int(severity_budget.get("medium", 999)):
            overall_ok = False

    return {
        "gate": "visual",
        "ok": overall_ok,
        "source_url": source_url,
        "page_name": page_name,
        "viewport_width": viewport_width,
        "aggregate_severity": aggregate_counts,
        "severity_budget": severity_budget,
        "shots": per_shot,
    }


# ── Driver ───────────────────────────────────────────────────────────────


async def _amain(app_code: str, client_code: str = "SYSTEM") -> int:
    # Load settings from the same config-server path the FastAPI agent uses,
    # so the standalone eval picks up the LIVE Anthropic key (not the stale
    # one in .env). Without this, gate_visual hits Anthropic with a dead key
    # and every diff fails to parse.
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from app.config import initialize_settings
        await initialize_settings()
    except Exception as e:  # noqa: BLE001
        print(f"  WARN: config-server bootstrap failed ({e}); falling back to .env values.")

    jwt = await _ensure_jwt()
    ts = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    outdir = EVALS_DIR / app_code / ts
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== eval {app_code} ===")
    print(f"    out: {outdir}")

    structural = await gate_structural(jwt, app_code, client_code)
    print(f"  [structural] {'PASS' if structural['ok'] else 'FAIL'}")
    if not structural["ok"] and "pages" in structural:
        for p in structural["pages"]:
            if not p["ok"]:
                print(f"    - {p['name']}: {p['detail'][:200]}")

    inventory = await gate_inventory(jwt, app_code, client_code)
    print(f"  [inventory ] {'PASS' if inventory['ok'] else 'FAIL'}")
    if "pages" in inventory:
        for p in inventory["pages"]:
            mark = "✓" if p["ok"] else "✗"
            print(f"    {mark} {p['name']}: {p['components']} components")

    render = await gate_render(app_code, client_code)
    print(f"  [render    ] {'PASS' if render['ok'] else 'FAIL'}")
    if "pages" in render:
        for p in render["pages"]:
            mark = "✓" if p["ok"] else "✗"
            err = f" pageerror={p['page_errors'][0][:80]}" if p["page_errors"] else ""
            print(f"    {mark} {p['name']}: text={p['body_text_chars']}c "
                  f"inputs={p['input_count']} btns={p['button_count']}{err}")
            # Write each screenshot to disk for inspection.
            if p.get("screenshot_b64"):
                png_path = outdir / f"render_{p['name']}.png"
                png_path.write_bytes(base64.b64decode(p["screenshot_b64"]))
                p.pop("screenshot_b64", None)
                p["screenshot_path"] = str(png_path.relative_to(REPO_ROOT))

    scenario = _load_scenario(app_code)
    visual = await gate_visual(app_code, client_code, scenario, outdir)
    status = visual.get("status")
    if status == "n/a":
        label = "SKIP"
    else:
        label = "PASS" if visual["ok"] else "FAIL"
    print(f"  [visual    ] {label}")
    if status == "n/a":
        print("    (no clone_target in scenario; gate skipped)")
    else:
        agg = visual.get("aggregate_severity") or {}
        budget = visual.get("severity_budget") or {}
        print(f"    severity: high={agg.get('high',0)} medium={agg.get('medium',0)} "
              f"low={agg.get('low',0)}  budget(high≤{budget.get('high',0)}, "
              f"medium≤{budget.get('medium',5)})")
        for shot in visual.get("shots") or []:
            if not shot.get("ok"):
                print(f"    ✗ scroll={shot.get('scroll')}: {shot.get('reason','?')[:120]}")
                continue
            sc = shot.get("severity_counts") or {}
            print(f"    • scroll={shot.get('scroll')}: H={sc.get('high',0)} "
                  f"M={sc.get('medium',0)} L={sc.get('low',0)} ({shot.get('diff_count',0)} diffs)")
            for d in (shot.get("top_diffs") or [])[:3]:
                sev = str(d.get("severity", "?")).upper()
                print(f"        [{sev}] {d.get('section','?')}: {d.get('fix_suggestion','')[:120]}")

    # Visual gate only blocks overall if it actually ran (clone scenarios).
    visual_blocks = visual["ok"] or status == "n/a"

    scorecard = {
        "app_code": app_code,
        "client_code": client_code,
        "timestamp": ts,
        "overall_ok": structural["ok"] and inventory["ok"] and render["ok"] and visual_blocks,
        "gates": {
            "structural": structural,
            "inventory": inventory,
            "render": render,
            "visual": visual,
        },
    }
    (outdir / "scorecard.json").write_text(json.dumps(scorecard, indent=2, default=str))
    latest = EVALS_DIR / app_code / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(outdir.name)

    print(f"\n  overall: {'PASS' if scorecard['overall_ok'] else 'FAIL'}")
    return 0 if scorecard["overall_ok"] else 1


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    app_code = argv[0]
    client_code = argv[1] if len(argv) > 1 else "SYSTEM"
    return asyncio.run(_amain(app_code, client_code))


if __name__ == "__main__":
    sys.exit(main())
