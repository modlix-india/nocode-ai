"""Cookie / consent banner dismissal.

Heuristic-first: find a fixed/sticky overlay whose text mentions cookies/consent
and click its accept (preferred) or dismiss button. Optional LLM-vision fallback
(per the user's directive #9) when the heuristic can't locate a button -- it is
the ONLY LLM use in the analyzer and is off unless `use_llm=True`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Tag the chosen button so Python can click it by a stable selector.
_FIND_BANNER_JS = r"""
() => {
  const TXT = /cookie|consent|gdpr|privacy|tracking|we use/i;
  const POSITIVE = /accept|agree|allow|got it|^\s*ok\s*$|okay|enable|continue|understand/i;
  const NEUTRAL = /decline|reject|dismiss|close|no thanks|necessary only/i;
  const cands = [];
  for (const el of document.querySelectorAll('div, section, aside, dialog, [role=dialog]')) {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if ((s.position === 'fixed' || s.position === 'sticky') && r.width > 1 && r.height > 1 &&
        s.display !== 'none' && s.visibility !== 'hidden') {
      const t = (el.textContent || '');
      if (TXT.test(t) && t.length < 1200) cands.push(el);
    }
  }
  if (!cands.length) return null;
  cands.sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return (ra.width * ra.height) - (rb.width * rb.height);
  });
  const banner = cands[0];
  const buttons = Array.from(banner.querySelectorAll('button, a, [role=button]'));
  let btn = buttons.find((b) => POSITIVE.test((b.textContent || '').trim()));
  if (!btn) btn = buttons.find((b) => NEUTRAL.test((b.textContent || '').trim()));
  if (!btn && buttons.length) btn = buttons[0];
  if (!btn) return null;
  btn.setAttribute('data-mxa-banner-btn', '1');
  return {
    button_text: (btn.textContent || '').trim().slice(0, 40),
    banner_text: (banner.textContent || '').trim().slice(0, 100),
  };
}
"""


async def dismiss_banner(
    page, *, use_llm: bool = False, llm_dismisser: Optional[Any] = None
) -> Dict[str, Any]:
    """Try to dismiss a cookie/consent overlay. Returns a small report."""
    try:
        found = await page.evaluate(_FIND_BANNER_JS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("banner find failed: %s", exc)
        found = None

    if found:
        try:
            await page.click('[data-mxa-banner-btn="1"]', timeout=3000)
            await page.wait_for_timeout(500)
            logger.info("dismissed banner via heuristic: %s", found.get("button_text"))
            return {"dismissed": True, "method": "heuristic", **found}
        except Exception as exc:  # noqa: BLE001
            logger.debug("banner click failed: %s", exc)

    if use_llm and llm_dismisser is not None:
        try:
            png = await page.screenshot(full_page=False, type="png")
            click = await llm_dismisser(png)  # -> {"x":int,"y":int} or None
            if click and "x" in click and "y" in click:
                await page.mouse.click(click["x"], click["y"])
                await page.wait_for_timeout(500)
                logger.info("dismissed banner via LLM vision at %s", click)
                return {"dismissed": True, "method": "llm", **(found or {})}
        except Exception as exc:  # noqa: BLE001
            logger.debug("llm banner fallback failed: %s", exc)

    return {"dismissed": False, "method": "none", **(found or {})}
