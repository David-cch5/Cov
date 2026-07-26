"""Shared Playwright browser-session lifecycle for county recorder/clerk
portal adapters (app/recorder/adapters/*.py) -- mirrors the role
app/gis/adapters/base_arcgis.py plays for the GIS adapters.

Unlike the GIS adapters, these portals have no stable public JSON API to hit
directly with `requests`: confirmed by testing that the GovOS "PublicSearch"
vendor's actual search results never appear in any interceptable
window.fetch/XMLHttpRequest call (something in its bundle serves them by a
mechanism that bypasses both), so a real rendered browser is genuinely
required here, not a REST client -- consistent with CLAUDE.md's stated tech
stack ("programmatic headless browser (Playwright) for recorder portals").
"""
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

DEFAULT_TIMEOUT_MS = 30000


@contextmanager
def recorder_context(headless: bool = True):
    """Yields a fresh Playwright BrowserContext. Adapters open their own
    page(s) from it -- some vendors (e.g. Acclaim) open a document detail
    view in a new tab/popup rather than navigating in place, which needs the
    context (not just a page) to observe via `expect_page()`."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1400, "height": 1200})
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        try:
            yield context
        finally:
            browser.close()
