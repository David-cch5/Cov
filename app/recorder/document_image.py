"""Retrieve a recorded document's IMAGE, not just its index row.

The missing half of document acquisition. This project could search recorder
portals and read index rows from the day the adapters were written, but it had
never fetched a page image -- `recorder_document_image` has existed unwritten
since the initial schema. Everything that needs to READ a document rather than
merely cite it waits on this: covid 4981 tract 3's omitted arc call, the Parcels
1201-09 plat that would settle its Phase III tie, and the 8386/4781/3428 tracts
whose readings are blocked on a better copy.

TWO RULES ABOUT CREDENTIALS, both structural rather than advisory.

  THE SECRET NEVER LEAVES .env. Credentials are read from the environment at the
  moment they are typed into the portal and are never logged, echoed into an
  error message, stored in the database, or written to a job payload. Every
  diagnostic in this module reports WHETHER a login was configured, never what
  it was.

  SEARCHING IS A GUEST OPERATION; ONLY IMAGES NEED A LOGIN. So this signs in
  lazily -- it walks to the document as a guest and authenticates only when the
  portal actually withholds the image. A pipeline that logs in on every search
  burns a shared account's session for no reason, and on these portals a
  concurrent-session limit is a real constraint (county_recorder_registry
  carries workers_allowed=1 for exactly this reason).

Credentials are keyed by VENDOR, not county: one GovOS PublicSearch account
covers Collin, Denton, Montgomery, Nueces and Ellis alike, which is why
app/config.py exposes a single PUBLICSEARCH_* pair rather than a per-county map.
"""
import os
import re

from app.config import OBJECT_STORAGE_ROOT, publicsearch_credentials
# Imported, never re-declared: the search box id is portal knowledge that was
# discovered once and belongs in one place. A second copy of it here would drift
# silently, and the first symptom would be a 30-second timeout that looks like an
# outage -- which is exactly how the first run of this module failed.
from app.recorder.adapters.publicsearch import SEARCH_BOX_ID
_SUBMIT = '[data-testid="searchSubmitButton"]'
_DOC_LINK = "a[href*='/doc/']"
_PAGE_IMAGE = "img[src*='/image'], img[src*='page'], canvas"
# Phrases these portals use when an image is behind the login or a purchase.
_GATED_RE = re.compile(
    r"sign\s*in|log\s*in|subscribe|unofficial\s+copy\s+unavailable|"
    r"create\s+an\s+account", re.IGNORECASE)
# A gate that wants MONEY, not just an account. Kept separate from _GATED_RE
# because the two call for opposite responses: one is ours to satisfy, the other
# is the user's decision to make.
# A recorded page image is hundreds of kilobytes. Anything smaller is site
# chrome, and saving it as a document page is a silent corruption of the record.
_MIN_PAGE_BYTES = 20_000

_COST_RE = re.compile(
    r"add\s+to\s+cart|checkout|purchase|\$\s*\d|price|per\s+page", re.IGNORECASE)


class DocumentImageUnavailable(Exception):
    """The image could not be retrieved. Carries WHY, so a caller can tell a
    login problem from a missing document from an outage -- the same distinction
    app/gis/ngs.py draws, and for the same reason: the three call for completely
    different responses and conflating them wastes money."""


class DocumentImageCosts(DocumentImageUnavailable):
    """The image is behind a PURCHASE, not merely a login.

    Raised rather than proceeding, always. Image cost varies by county on this
    vendor -- Collin serves them free to a signed-in account while others charge
    per page -- and a fetcher that completes a checkout because it could would be
    spending money on its own initiative. county_recorder_registry.quirks carries
    image_cost per county; anything but 'free' stops here and asks.
    """


class PortalLoginRequired(DocumentImageUnavailable):
    """The portal is withholding the image pending authentication, and no
    credentials are configured. Reports the env var names to set, never a value."""


def _document_dir(county_fips: str, doc_number: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", doc_number)
    path = os.path.join(OBJECT_STORAGE_ROOT, "recorder_images", county_fips, safe)
    os.makedirs(path, exist_ok=True)
    return path


def sign_in(page, base_url: str) -> bool:
    """Authenticate against a GovOS PublicSearch portal.

    Returns True on success, False when no credentials are configured. Raises
    only on a portal that accepted neither. NOTHING here logs either credential:
    the values pass from the environment into page.fill and are never formatted
    into a string.
    """
    credentials = publicsearch_credentials()
    if credentials is None:
        return False
    username, password = credentials
    # THE PATH IS /signin. Both /login and /sign-in resolve and contain ZERO
    # input elements -- they are not the form, and guessing at plausible paths
    # cost two attempts before the header's own link answered it. The header
    # carries <a href="/signin?returnPath=%2F">Sign In</a>, and that page has
    # #email (data-testid loginEmail), #password, and a loginButton. Discovered
    # from the live DOM rather than assumed, which is the only way these portals
    # ever get pinned down.
    page.goto(base_url.rstrip("/") + "/signin", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    for user_selector in ("[data-testid='loginEmail']", "#email", "input[type='email']",
                          "input[name='email']", "input[name='username']"):
        if page.query_selector(user_selector):
            page.fill(user_selector, username)
            break
    else:
        raise DocumentImageUnavailable(
            f"{base_url}: no username field found on the sign-in page -- the portal's "
            f"login form has changed shape, so this needs re-discovery (credentials WERE "
            f"configured, so this is not a missing-login problem)")
    for password_selector in ("input[name='password']", "input[type='password']", "#password"):
        if page.query_selector(password_selector):
            page.fill(password_selector, password)
            break
    else:
        raise DocumentImageUnavailable(f"{base_url}: no password field on the sign-in page")
    for submit in ("[data-testid='loginButton']", "button[type='submit']",
                   "text=Sign In", "text=Log in"):
        if page.query_selector(submit):
            page.click(submit)
            break
    page.wait_for_timeout(4000)
    signed_in = not _GATED_RE.search(page.inner_text("body")[:4000] or "")
    if not signed_in:
        raise DocumentImageUnavailable(
            f"{base_url}: sign-in did not take -- the credentials in "
            f"PUBLICSEARCH_USERNAME/PUBLICSEARCH_PASSWORD were rejected, or the portal "
            f"is showing a further gate (a concurrent-session limit looks like this too)")
    return True


def _select_department(page, department: str) -> bool:
    """Switch the portal's Department combobox, the way it actually works.

    Not a `text=Plats` click, which is what I wrote first and what silently
    no-ops: the control is a react-select combobox with no native <select>, and
    the option elements do not exist in the DOM until the control is OPENED. The
    clickable element is the DIV whose class ends in "-control" -- several other,
    non-clickable DIVs carry the same label text, so a plain text locator can
    match one of those instead and do nothing at all.

    publicsearch.py's search_plats_by_subdivision discovered all of this against
    the live DOM and documents it at length; this is the same sequence, and the
    right long-term move is for both to share one helper rather than agree by
    inspection.

    Returns False rather than raising when the department is absent -- not every
    county on this vendor has a Plats department (Nueces files plats in its
    default department as MAP and PLAT doc types), and a caller that gets False
    can search elsewhere.
    """
    control = page.query_selector("div[class$='-control']")
    if control is None:
        return False
    control.click()
    page.wait_for_timeout(400)
    options = [o.inner_text().strip() for o in page.query_selector_all("div[id*='option']")]
    if department not in options:
        page.keyboard.press("Escape")
        return False
    try:
        page.click(f"text={department}", timeout=5000)
    except Exception:                                            # noqa: BLE001
        return False
    page.wait_for_timeout(400)
    return True


def _run_search(page, base_url: str, query: str, department: str | None) -> bool:
    """Type a query into the portal's own form. True if a result table appeared."""
    page.goto(base_url, wait_until="networkidle")
    page.wait_for_timeout(1200)
    if department and not _select_department(page, department):
        return False
    page.fill(f"#{SEARCH_BOX_ID}", query)
    page.click(_SUBMIT)
    try:
        page.wait_for_selector("table", timeout=25000)
        return True
    except Exception:                                            # noqa: BLE001
        return False


def _matching_row(page, doc_number: str):
    """The result row whose text carries this document number."""
    for row in page.query_selector_all("tbody tr"):
        try:
            if doc_number in (row.inner_text() or ""):
                return row
        except Exception:                                        # noqa: BLE001
            continue
    return None


def open_document(page, base_url: str, doc_number: str,
                  fallback_query: str | None = None) -> str:
    """Walk to a document's viewer THROUGH THE SEARCH FORM and return its URL.

    Deliberately not a constructed /results?q=... URL: measured against Collin,
    a bare results URL answers with a page titled "Error" and no document links
    at all, while the same query typed into the form works. These portals are
    single-page apps that build their result state client-side, so the form is
    the supported entry point and a hand-made URL is not.
    """
    if not _run_search(page, base_url, doc_number, department=None):
        # A DOCUMENT NUMBER IS NOT ALWAYS SEARCHABLE. Collin's quick search
        # returns nothing for plat 20021119001712550 while its Plats department
        # returns that very row -- the number is in the index but not in the
        # field the default search covers. So fall back to the department search
        # that found the document in the first place and pick the row out by
        # number, rather than concluding the document does not exist.
        if not (fallback_query and _run_search(page, base_url, fallback_query,
                                               department="Plats")):
            raise DocumentImageUnavailable(
                f"{base_url}: no result table for document {doc_number}"
                + (f" (nor for {fallback_query!r} in Plats)" if fallback_query else
                   " -- pass fallback_query with the subdivision name if this document "
                   "is only reachable through a department search"))
    # THE ROW IS THE LINK. GovOS result rows carry no <a> at all -- the <tr>
    # itself is clickable and the app routes to /doc/<id> in JavaScript. Looking
    # for an anchor finds nothing and reads like "the document has no image",
    # which is a very different and much more alarming conclusion than "this
    # table is built differently than I assumed".
    row = _matching_row(page, doc_number)
    if row is None:
        raise DocumentImageUnavailable(
            f"{base_url}: searched successfully but no result row carries document "
            f"{doc_number}")
    link = row.query_selector("a[href*='/doc/']")
    if link is not None:
        href = link.get_attribute("href") or ""
        url = href if href.startswith("http") else base_url.rstrip("/") + href
        page.goto(url, wait_until="domcontentloaded")
    else:
        row.click()
        page.wait_for_timeout(1500)
        try:
            page.wait_for_url("**/doc/**", timeout=20000)
        except Exception as exc:                                 # noqa: BLE001
            raise DocumentImageUnavailable(
                f"{base_url}: clicking document {doc_number}'s row did not open a viewer "
                f"(landed on {page.url})") from exc
    page.wait_for_timeout(5000)
    return page.url


def save_document_pages(page, county_fips: str, doc_number: str) -> list[str]:
    """Save every page image the viewer exposes. Returns the files written.

    Falls back to a full-page screenshot of the viewer when the images are drawn
    to a canvas rather than served as <img> -- a screenshot of a plat is worth
    having even when it is not the archival image, because a plat's own recited
    bearings are readable from it and that is what the geometry work needs.
    """
    directory = _document_dir(county_fips, doc_number)
    written: list[str] = []
    sources = [s for s in page.eval_on_selector_all(
        _PAGE_IMAGE, "els => els.map(e => e.src || '')") if s and "logo" not in s.lower()]
    for number, source in enumerate(sources, start=1):
        try:
            response = page.request.get(source)
            if not response.ok:
                continue
            body = response.body()
            if len(body) < _MIN_PAGE_BYTES:
                # An icon, not a page. The viewer's <img> elements include chrome
                # -- the first real attempt here saved a 583-byte and a 936-byte
                # file and reported success, which is worse than failing: a
                # caller downstream would have tried to read a plat off a button.
                continue
            path = os.path.join(directory, f"page_{number:02d}.png")
            with open(path, "wb") as handle:
                handle.write(body)
            written.append(path)
        except Exception:                                        # noqa: BLE001
            continue
    if not written:
        # These viewers commonly draw pages to a <canvas> or serve them as tiles,
        # so there is no image to fetch. A full-page screenshot of the viewer is
        # not the archival image and is not pretending to be -- but a plat's own
        # recited bearings and distances are legible from it, which is what the
        # geometry work actually needs.
        path = os.path.join(directory, "viewer_full_page.png")
        page.screenshot(path=path, full_page=True)
        written.append(path)
    return written


_DOWNLOAD_SELECTORS = ("text=Download (Free)", "[data-testid='downloadButton']",
                       "text=Download")


def download_document(page, county_fips: str, doc_number: str) -> str | None:
    """Use the viewer's own Download control, when the county serves it free.

    Preferred over scraping page images: it returns the whole instrument in one
    file at full resolution, which is what reading a plat's recited bearings
    actually requires -- a viewer screenshot is legible for a cover sheet and not
    for field notes. Only ever clicked when the caller has established the county
    serves images free (see DocumentImageCosts); a control labelled "Download"
    that turns out to cost money must not be clicked speculatively.
    """
    directory = _document_dir(county_fips, doc_number)
    for selector in _DOWNLOAD_SELECTORS:
        if page.query_selector(selector) is None:
            continue
        try:
            with page.expect_download(timeout=120_000) as download:
                page.click(selector)
            saved = download.value
            suggested = saved.suggested_filename or f"{doc_number}.pdf"
            path = os.path.join(directory, suggested)
            saved.save_as(path)
            return path
        except Exception:                                        # noqa: BLE001
            continue
    return None


def fetch_document_image(context, county_fips: str, base_url: str, doc_number: str,
                         fallback_query: str | None = None,
                         image_cost: str | None = None) -> dict:
    """Retrieve a document's pages, signing in only if the portal withholds them.

    Returns {"files": [...], "signed_in": bool, "viewer_url": str}. Raises
    PortalLoginRequired when the image is gated and no credentials are set --
    which is a configuration answer, not a dead end, and says which two
    environment variables to fill.
    """
    page = context.new_page()
    try:
        viewer = open_document(page, base_url, doc_number, fallback_query)
        body = page.inner_text("body")[:4000] or ""
        signed_in = False
        if _COST_RE.search(body) and (image_cost or "unknown") != "free":
            raise DocumentImageCosts(
                f"{base_url}: document {doc_number}'s image appears to be behind a purchase "
                f"and this county's image_cost is {image_cost or 'unknown'!r}. Not proceeding "
                f"-- set quirks.image_cost='free' for a county whose images are included, or "
                f"retrieve this one manually. Nothing here will complete a checkout.")
        if _GATED_RE.search(body):
            if publicsearch_credentials() is None:
                raise PortalLoginRequired(
                    f"{base_url}: document {doc_number}'s image is behind a login and no "
                    f"credentials are configured. Set PUBLICSEARCH_USERNAME and "
                    f"PUBLICSEARCH_PASSWORD in .env (one GovOS account covers every "
                    f"*.publicsearch.us county). Searching does not need them; images do.")
            signed_in = sign_in(page, base_url)
            viewer = open_document(page, base_url, doc_number, fallback_query)
        downloaded = (download_document(page, county_fips, doc_number)
                      if (image_cost == "free") else None)
        files = ([downloaded] if downloaded
                 else save_document_pages(page, county_fips, doc_number))
        return {"files": files, "signed_in": signed_in, "viewer_url": viewer,
                "downloaded": bool(downloaded),
                "doc_number": doc_number, "county_fips": county_fips}
    finally:
        page.close()
