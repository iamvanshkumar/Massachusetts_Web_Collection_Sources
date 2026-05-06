"""
Massachusetts Trial Court Case Access — Search by Case Type
Flow (incremental build):
  Step 1: Open home page → solve reCAPTCHA → click "Click Here" to enter search
  Step 2: Fill search form — Court Department, Court Division, Number of Results
  Step 3: (coming) Paginate results and extract case data
"""

import time
import random
import base64
import io
import os
import csv
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from PIL import Image
from google import genai
from google.genai import types

# ============================================================================
# Configuration
# ============================================================================

HOME_URL     = "https://www.masscourts.org/eservices/home.page.2"
LOG_DIR      = "logs"

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable not set")

genai_client = genai.Client(api_key=API_KEY)
GEMINI_MODEL = "gemini-2.5-flash"

# ============================================================================
# Logger
# ============================================================================

def setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    fmt      = "%(asctime)s  %(levelname)-8s  %(message)s"
    datefmt  = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(
        level=logging.INFO, format=fmt, datefmt=datefmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.info(f"Log file: {log_file}")

log = logging.getLogger(__name__)

# ============================================================================
# Browser helpers
# ============================================================================

def human_delay(min_sec=0.8, max_sec=2.2):
    time.sleep(random.uniform(min_sec, max_sec))

def human_move_and_click(driver, element):
    ActionChains(driver)\
        .move_to_element(element)\
        .pause(random.uniform(0.3, 0.7))\
        .click()\
        .perform()

# ============================================================================
# reCAPTCHA — Checkbox
# ============================================================================

def wait_for_recaptcha_iframe(driver, timeout=60):
    """
    Wait for the reCAPTCHA checkbox iframe to appear, be visible, AND have
    its src fully loaded (contains the sitekey). The captcha on this site
    loads late so we poll for up to 60 seconds.
    """
    SITEKEY = "6Ld9CuYlAAAAAPTI6mtGcd0843Pj2knpu9VsmS7Y"
    log.info("[CAPTCHA] Waiting for reCAPTCHA iframe to load (up to 60s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[title='reCAPTCHA']")
            for iframe in iframes:
                src = iframe.get_attribute("src") or ""
                if SITEKEY in src and iframe.is_displayed():
                    log.info("[CAPTCHA] reCAPTCHA iframe fully loaded.")
                    return iframe
        except Exception:
            pass
        time.sleep(1.0)
    log.warning("[CAPTCHA] reCAPTCHA iframe did not appear within timeout.")
    return None


def click_recaptcha_checkbox(driver, iframe):
    """Switch into the reCAPTCHA iframe and click the checkbox."""
    SITEKEY = "6Ld9CuYlAAAAAPTI6mtGcd0843Pj2knpu9VsmS7Y"
    try:
        # Re-find the iframe fresh right before switching to avoid stale ref
        fresh = None
        for f in driver.find_elements(By.CSS_SELECTOR, "iframe[title='reCAPTCHA']"):
            src = f.get_attribute("src") or ""
            if SITEKEY in src and f.is_displayed():
                fresh = f
                break
        target = fresh if fresh else iframe

        driver.switch_to.frame(target)

        # Wait for the checkbox to be present inside the frame
        checkbox = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "recaptcha-anchor"))
        )
        human_delay(0.8, 1.5)

        # Use JS click — more reliable than ActionChains inside cross-origin iframe
        driver.execute_script("arguments[0].click();", checkbox)
        log.info("[CAPTCHA] Clicked checkbox.")
        driver.switch_to.default_content()
        return True
    except Exception as e:
        log.error(f"[ERROR] Checkbox click failed: {e}")
        driver.switch_to.default_content()
        return False


def is_checkbox_solved(driver):
    """Return True if the reCAPTCHA checkbox shows aria-checked=true."""
    try:
        iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[title='reCAPTCHA']")
        if not iframes:
            return False
        driver.switch_to.frame(iframes[0])
        anchor  = driver.find_element(By.ID, "recaptcha-anchor")
        checked = anchor.get_attribute("aria-checked") == "true"
        driver.switch_to.default_content()
        return checked
    except Exception:
        driver.switch_to.default_content()
        return False

# ============================================================================
# reCAPTCHA — Image Challenge (bframe)
# ============================================================================

def get_challenge_iframe(driver, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            iframes = driver.find_elements(
                By.XPATH,
                "//iframe[contains(@src,'bframe') or "
                "@title='recaptcha challenge expires in two minutes']"
            )
            for iframe in iframes:
                if iframe.is_displayed():
                    return iframe
        except Exception:
            pass
        time.sleep(0.5)
    return None


def get_task_text(driver):
    try:
        el = driver.find_element(
            By.CSS_SELECTOR,
            ".rc-imageselect-desc-no-canonical, .rc-imageselect-desc"
        )
        return el.text.strip()
    except Exception:
        return "the requested object"


def screenshot_challenge_iframe(driver, challenge_iframe,
                                output_path="captcha_challenge_debug.png"):
    try:
        screenshot_bytes = driver.get_screenshot_as_png()
        screenshot = Image.open(io.BytesIO(screenshot_bytes))

        loc  = challenge_iframe.location
        size = challenge_iframe.size
        dpr  = driver.execute_script("return window.devicePixelRatio") or 1

        x1 = int(loc["x"] * dpr)
        y1 = int(loc["y"] * dpr)
        x2 = int((loc["x"] + size["width"])  * dpr)
        y2 = int((loc["y"] + size["height"]) * dpr)

        cropped = screenshot.crop((x1, y1, x2, y2))
        cropped.save(output_path)
        log.info(f"[CAPTCHA] Challenge screenshot saved: {output_path}")

        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return base64.standard_b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        log.error(f"[ERROR] Screenshot failed: {e}")
        return None


def ask_gemini(base64_image, task_text, grid_size):
    cols   = 4 if grid_size == 16 else 3
    rows   = grid_size // cols
    prompt = (
        f"This is a Google reCAPTCHA image challenge. "
        f"The grid has {rows} rows and {cols} columns, with tiles numbered 0 to {grid_size-1} "
        f"left-to-right, top-to-bottom (0=top-left, {cols-1}=top-right, {grid_size-1}=bottom-right).\n\n"
        f"Task: \"{task_text}\"\n\n"
        f"Look carefully at each tile and identify which ones match the task.\n"
        f"RESPOND ONLY with a Python list of integers (0-indexed). "
        f"Examples: [0, 3, 7] or [1, 5] or []\n"
        f"No explanations, no markdown, just the list."
    )

    models     = [GEMINI_MODEL, "gemini-2.0-flash", "gemini-1.5-flash-8b"]
    image_data = base64.b64decode(base64_image)
    image_part = types.Part.from_bytes(data=image_data, mime_type="image/png")

    for attempt, model in enumerate(models):
        try:
            response = genai_client.models.generate_content(
                model=model,
                contents=[image_part, prompt],
            )
            raw    = response.text.strip()
            log.info(f"[AI] Gemini ({model}): {raw}")
            result = eval(raw)
            if isinstance(result, list) and all(isinstance(x, int) for x in result):
                return result
        except Exception as e:
            log.info(f"[AI] Attempt {attempt+1} failed: {str(e)[:100]}")
            time.sleep(5)
    return []


def click_tiles(driver, tile_ids):
    clicked = 0
    for tid in tile_ids:
        try:
            td = driver.find_element(By.CSS_SELECTOR, f"td[id='{tid}']")
            human_delay(0.4, 0.9)
            driver.execute_script("arguments[0].click();", td)
            log.info(f"[CAPTCHA] Clicked tile id={tid}")
            clicked += 1
        except Exception as e:
            log.warning(f"[WARNING] Could not click tile {tid}: {e}")
    return clicked


def click_verify_button(driver):
    try:
        btn = driver.find_element(By.ID, "recaptcha-verify-button")
        human_delay(0.8, 1.5)
        driver.execute_script("arguments[0].click();", btn)
        log.info("[CAPTCHA] Clicked Verify.")
        return True
    except Exception as e:
        log.error(f"[ERROR] Verify button not found: {e}")
        return False


def solve_image_challenge(driver, challenge_iframe, max_rounds=6):
    for round_num in range(1, max_rounds + 1):
        log.info(f"[CAPTCHA] Image challenge round {round_num}...")
        driver.switch_to.default_content()

        challenge_iframe = get_challenge_iframe(driver, timeout=10)
        if not challenge_iframe:
            log.info("[CAPTCHA] Challenge iframe gone — solved!")
            return True

        b64 = screenshot_challenge_iframe(driver, challenge_iframe)
        if not b64:
            return False

        driver.switch_to.frame(challenge_iframe)
        human_delay(1.5, 2.5)

        task_text = get_task_text(driver)
        log.info(f"[CAPTCHA] Task: '{task_text}'")

        tiles     = driver.find_elements(By.CSS_SELECTOR, "td.rc-imageselect-tile")
        grid_size = len(tiles) if tiles else 9
        log.info(f"[CAPTCHA] Grid: {grid_size} tiles")

        tile_ids = ask_gemini(b64, task_text, grid_size)
        log.info(f"[CAPTCHA] Tiles to click: {tile_ids}")

        if tile_ids:
            click_tiles(driver, tile_ids)
            human_delay(1.0, 2.0)
            click_verify_button(driver)
        else:
            log.info("[CAPTCHA] No tiles selected, clicking verify to get new challenge...")
            click_verify_button(driver)

        driver.switch_to.default_content()
        human_delay(2.0, 3.5)

        if is_checkbox_solved(driver):
            log.info("[CAPTCHA] Checkbox confirmed solved!")
            return True

        if not get_challenge_iframe(driver, timeout=3):
            log.info("[CAPTCHA] Challenge closed — solved!")
            return True

    log.warning("[WARNING] Max challenge rounds reached.")
    return False


def handle_recaptcha(driver):
    """Full reCAPTCHA flow: checkbox → optional image challenge."""
    log.info("[CAPTCHA] Looking for reCAPTCHA widget...")
    iframe = wait_for_recaptcha_iframe(driver, timeout=20)
    if not iframe:
        log.info("[CAPTCHA] No reCAPTCHA found, skipping.")
        return True

    if not click_recaptcha_checkbox(driver, iframe):
        return False

    human_delay(2.0, 3.0)

    if is_checkbox_solved(driver):
        log.info("[CAPTCHA] Instant pass — no image challenge needed.")
        return True

    log.info("[CAPTCHA] Checking for image challenge...")
    challenge_iframe = get_challenge_iframe(driver, timeout=8)
    if not challenge_iframe:
        for _ in range(10):
            time.sleep(1)
            if is_checkbox_solved(driver):
                log.info("[CAPTCHA] Delayed instant pass.")
                return True
        log.warning("[WARNING] Neither instant pass nor image challenge detected.")
        return False

    log.info("[CAPTCHA] Image challenge detected — solving with Gemini...")
    return solve_image_challenge(driver, challenge_iframe)

# ============================================================================
# Wicket JS-redirect page handler
# ============================================================================

def handle_wicket_redirect(driver, timeout=8):
    """
    Detect the Wicket JS-redirect splash page that sometimes appears before
    the real home page loads:

        <body onload="javascript:submitform();">
        If you see this ... Please click <a href="?x=...">this link</a>

    Normally the page auto-submits via onload, but if JS is slow or the form
    hasn't fired yet we click the fallback anchor ourselves.
    Returns True if the redirect page was detected (and handled), False if
    the page looks normal and no action was needed.
    """
    try:
        # Quick check — is the body's onload the submitform redirect?
        body = driver.find_element(By.TAG_NAME, "body")
        onload = body.get_attribute("onload") or ""
        if "submitform" not in onload:
            return False  # normal page, nothing to do

        log.info("[NAV] Wicket JS-redirect page detected.")

        # Give the auto-submit a moment to fire on its own
        time.sleep(3)

        # If we're still on the redirect page, click the fallback link
        try:
            body2 = driver.find_element(By.TAG_NAME, "body")
            if "submitform" in (body2.get_attribute("onload") or ""):
                link = driver.find_element(
                    By.XPATH, "//a[contains(text(),'this link')]"
                )
                link.click()
                log.info("[NAV] Clicked fallback 'this link' on redirect page.")
        except Exception:
            pass  # already navigated away

        # Wait until we leave the redirect page
        WebDriverWait(driver, timeout).until(
            lambda d: "submitform" not in (
                d.find_element(By.TAG_NAME, "body").get_attribute("onload") or ""
            )
        )
        log.info(f"[NAV] Past redirect page. URL: {driver.current_url}")
        return True

    except Exception:
        return False  # not the redirect page or already past it


# ============================================================================
# Step 1 — Open home page, solve captcha, click "Click Here"
# ============================================================================

def open_and_enter_site(driver, wait):
    """
    1. Load the home page (handling Wicket JS-redirect splash if present)
    2. Solve the reCAPTCHA checkbox (+ image challenge if triggered)
    3. Click the 'Click Here' anchor (name='linkFrag:beginButton')
    4. Wait for the next page to load
    Returns True on success.
    """
    log.info(f"[NAV] Loading: {HOME_URL}")
    driver.get(HOME_URL)
    human_delay(2.0, 3.5)

    # Handle the Wicket JS-redirect splash page if it appears
    handle_wicket_redirect(driver)
    human_delay(1.0, 2.0)

    # Wait for the captcha wrapper div to appear in the DOM before attempting
    # to solve — the reCAPTCHA iframe loads asynchronously on this site.
    try:
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.g-recaptcha"))
        )
        log.info("[CAPTCHA] reCAPTCHA wrapper found in DOM.")
        human_delay(1.5, 2.5)   # give the iframe inside it time to fully render
    except Exception:
        log.info("[CAPTCHA] No reCAPTCHA wrapper found — may not be required.")

    # Solve reCAPTCHA
    if not handle_recaptcha(driver):
        log.error("[ERROR] Could not solve reCAPTCHA on home page.")
        return False

    human_delay(1.0, 2.0)

    # Click "Click Here" — locate by the span text inside the anchor,
    # falling back to name attribute in case Wicket re-renders the id.
    try:
        click_here_btn = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH,
                 "//a[contains(@class,'anchorButton') and "
                 ".//span[normalize-space(text())='Click Here'] and "
                 "contains(@onclick,'beginButton')]")
            )
        )
        log.info("[NAV] Found 'Click Here' button.")
        human_delay(0.5, 1.0)
        human_move_and_click(driver, click_here_btn)
        log.info("[NAV] Clicked 'Click Here'.")
    except Exception as e:
        log.error(f"[ERROR] Could not find/click 'Click Here': {e}")
        return False

    # Wait for the page to transition away from the welcome page
    try:
        WebDriverWait(driver, 30).until(
            EC.none_of(EC.url_contains("home.page"))
        )
        log.info(f"[NAV] Navigated to: {driver.current_url}")
    except Exception:
        # Wicket may do an in-page AJAX update rather than a full URL change;
        # wait for the welcome header to disappear instead.
        try:
            WebDriverWait(driver, 20).until(
                EC.invisibility_of_element_located((By.ID, "welcomePageHeader"))
            )
            log.info(f"[NAV] Welcome page replaced (AJAX). Current URL: {driver.current_url}")
        except Exception as e2:
            log.warning(f"[NAV] Page transition unclear: {e2}")

    human_delay(2.0, 3.0)
    return True

# ============================================================================
# Output
# ============================================================================

OUTPUT_DIR = "output"

def append_results_to_csv(rows, search_meta):
    """Append extracted result rows to output/results.csv."""
    if not rows:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath     = os.path.join(OUTPUT_DIR, "results.csv")
    write_header = not os.path.exists(filepath)

    fieldnames = [
        "CourtDepartment", "CourtDivision", "SearchBeginDate", "SearchEndDate",
        "PartyCompany", "CaseNumber", "CaseType", "FileDate",
        "InitiatingAction", "PartyType", "DateOfBirth", "CaseStatus", "Court", "Affiliation"
    ]
    with open(filepath, "a", newline="", encoding="utf-8", buffering=1) as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({**search_meta, **row})
            f.flush()
    log.info(f"[SAVE] {len(rows)} row(s) → {filepath}")


# ============================================================================
# Results page — extraction + pagination + smart date splitting
# ============================================================================

def get_result_count(driver):
    """
    Parse the result count from the page.
    Returns (shown, total) e.g. (75, 206) or (69, 69).
    Returns (0, 0) if not found.
    """
    try:
        # "Showing 1 to 69 of 69"  or  "Returning 100 of 206 records."
        texts = []
        for sel in ["#id14a", "#srchResultNotice", ".navigatorLabel span"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                texts.append(el.text.strip())
            except Exception:
                pass

        for text in texts:
            # "Showing X to Y of Z"
            m = re.search(r"Showing\s+\d+\s+to\s+(\d+)\s+of\s+(\d+)", text, re.I)
            if m:
                return int(m.group(1)), int(m.group(2))
            # "Returning X of Y records"
            m = re.search(r"Returning\s+(\d+)\s+of\s+(\d+)", text, re.I)
            if m:
                return int(m.group(1)), int(m.group(2))
            # "Showing 1 to X of X"
            m = re.search(r"of\s+(\d+)", text, re.I)
            if m:
                n = int(m.group(1))
                return n, n
    except Exception:
        pass
    return 0, 0


def extract_results_from_page(driver):
    """
    Parse all result rows from the current results page.
    Returns list of dicts with keys matching the CSV fieldnames.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(driver.page_source, "html.parser")
    table = soup.find("table", id="grid")
    if not table:
        return []

    rows = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 11:
            continue

        def cell_text(td):
            return td.get_text(" ", strip=True)

        rows.append({
            "PartyCompany":     cell_text(tds[2]),
            "CaseNumber":       cell_text(tds[3]),
            "CaseType":         cell_text(tds[4]),
            "FileDate":         cell_text(tds[5]),
            "InitiatingAction": cell_text(tds[6]),
            "PartyType":        cell_text(tds[7]),
            "DateOfBirth":      cell_text(tds[8]),
            "CaseStatus":       cell_text(tds[9]),
            "Court":            cell_text(tds[10]),
            "Affiliation":      cell_text(tds[11]) if len(tds) > 11 else "",
        })
    return rows


def paginate_and_collect(driver, wait):
    """
    Collect all rows across all pages of the current results.
    Uses the next-page navigator (span title='Go to next page').
    Returns list of row dicts.
    """
    all_rows = []
    page_num = 1

    while True:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table#grid tbody tr"))
        )
        human_delay(1.0, 1.5)

        rows = extract_results_from_page(driver)
        all_rows.extend(rows)
        log.info(f"[PAGE {page_num}] Extracted {len(rows)} rows (total so far: {len(all_rows)})")

        # Check for an active next-page button
        try:
            next_btn = driver.find_element(
                By.XPATH,
                "//span[@title='Go to next page' and not(contains(@class,'disabled'))]"
            )
            # Verify it's actually clickable (has an onclick or is inside an <a>)
            parent = next_btn.find_element(By.XPATH, "..")
            if parent.tag_name == "a" or next_btn.get_attribute("onclick"):
                driver.execute_script("arguments[0].click();", next_btn)
                human_delay(2.0, 3.0)
                page_num += 1
            else:
                break
        except Exception:
            break

    return all_rows


def date_range_chunks(begin_str, end_str, chunk="week"):
    """
    Split a date range (MM/DD/YYYY strings) into sub-ranges.
    chunk = 'week' → 7-day chunks
    chunk = 'day'  → 1-day chunks
    Yields (begin_str, end_str) pairs.
    """
    fmt = "%m/%d/%Y"
    start = datetime.strptime(begin_str, fmt)
    end   = datetime.strptime(end_str,   fmt)
    delta = timedelta(days=6 if chunk == "week" else 0)

    current = start
    while current <= end:
        chunk_end = min(current + delta, end)
        yield current.strftime(fmt), chunk_end.strftime(fmt)
        current = chunk_end + timedelta(days=1)


def run_one_search(driver, wait, row):
    """
    Navigate to the search page (using 'Revise Current Search' if already on
    results, or directly to the search URL), fill the qualifier form
    (Department → Division → page size), click the Case Type tab, fill that
    form, and submit.  Returns True on success.
    """
    # If we're on the results page, click "Revise Current Search" to go back
    # to the search form without losing the session.  Otherwise navigate directly.
    try:
        revise_link = driver.find_element(
            By.XPATH,
            "//a[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            "'abcdefghijklmnopqrstuvwxyz'),'revise current search')]"
        )
        driver.execute_script("arguments[0].click();", revise_link)
        log.info("[NAV] Clicked 'Revise Current Search'.")
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.NAME, "sdeptCd"))
        )
        human_delay(1.5, 2.5)
    except Exception:
        # Not on results page — fill the qualifier form that's already visible
        pass

    # ── Qualifier form: Department → Division → page size ────────────────────
    dept_display = row.get("CourtDepartments", "").strip()
    div_display  = row.get("CourtDivision", "").strip()

    dept_value = DEPT_VALUE_MAP.get(dept_display)
    if not dept_value:
        log.error(f"[FORM] Unknown department '{dept_display}'.")
        return False

    try:
        dept_select_el = wait.until(EC.presence_of_element_located((By.NAME, "sdeptCd")))
        wicket_select(driver, dept_select_el, dept_value, by_value=True)
        log.info(f"[FORM] Selected department: {dept_display}")
    except Exception as e:
        log.error(f"[FORM] Could not select department: {e}")
        return False

    try:
        div_select_el = WebDriverWait(driver, 15).until(
            EC.visibility_of_element_located((By.NAME, "sdivCd"))
        )
        wicket_select(driver, div_select_el, div_display, by_value=False)
        log.info(f"[FORM] Selected division: {div_display}")
    except Exception as e:
        log.error(f"[FORM] Could not select division '{div_display}': {e}")
        return False

    try:
        page_size_el = wait.until(EC.presence_of_element_located((By.NAME, "pageSize")))
        wicket_select(driver, page_size_el, "75", by_value=False)
        log.info("[FORM] Set results per page to 75.")
    except Exception as e:
        log.error(f"[FORM] Could not set page size: {e}")
        return False

    # ── Case Type tab ─────────────────────────────────────────────────────────
    try:
        case_type_tab = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//ul/li/a[.//span[normalize-space(text())='Case Type']]")
            )
        )
        driver.execute_script("arguments[0].click();", case_type_tab)
        WebDriverWait(driver, 15).until(
            lambda d: "selected" in (
                d.find_element(
                    By.XPATH,
                    "//ul/li[.//span[normalize-space(text())='Case Type']]"
                ).get_attribute("class") or ""
            )
        )
        human_delay(1.0, 2.0)
        log.info("[FORM] 'Case Type' tab active.")
    except Exception as e:
        log.error(f"[FORM] Could not click 'Case Type' tab: {e}")
        return False

    # ── Case Type tab form + submit ───────────────────────────────────────────
    return fill_case_type_tab(driver, wait, row)


def collect_with_smart_split(driver, wait, row, begin_date, end_date, depth=0):
    """
    Recursively collect results, splitting the date range if >100 records.
    depth 0 = month range, depth 1 = week chunks, depth 2 = day chunks.
    Returns list of row dicts.
    """
    MAX_DEPTH = 2
    CHUNK_NAMES = ["month", "week", "day"]

    log.info(f"[SPLIT] Searching {begin_date} → {end_date} "
             f"(depth={depth}, chunk={CHUNK_NAMES[depth]})")

    # Fill and submit the form for this date range
    sub_row = {**row, "FilingDateFrom": begin_date, "FilingDateTo": end_date}
    # Dates already in MM/DD/YYYY — pass directly (convert_date will pass through)
    ok = run_one_search(driver, wait, sub_row)
    if not ok:
        log.warning(f"[SPLIT] Form fill failed for {begin_date}→{end_date}")
        return []

    # Wait for results
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table#grid, #srchResultNotice"))
        )
        human_delay(1.5, 2.5)
    except Exception:
        pass

    shown, total = get_result_count(driver)
    log.info(f"[SPLIT] Results: shown={shown}, total={total}")

    if total <= 100 or depth >= MAX_DEPTH:
        # Collect all pages
        return paginate_and_collect(driver, wait)

    # Need to split further
    next_chunk = "week" if depth == 0 else "day"
    all_rows = []
    for chunk_begin, chunk_end in date_range_chunks(begin_date, end_date, chunk=next_chunk):
        chunk_rows = collect_with_smart_split(
            driver, wait, row, chunk_begin, chunk_end, depth=depth + 1
        )
        all_rows.extend(chunk_rows)
        human_delay(1.0, 2.0)
    return all_rows

SEARCH_CSV = "sample_data.csv"

# Maps CSV display names → <option value> for Case Type (name="caseCd")
CASE_TYPE_VALUE_MAP = {
    "Civil":                  "CV                            ",
    "Criminal":               "CR                            ",
    "Criminal Cross Site":    "CRX                           ",
    "Drug Court":             "DTX                           ",
    "Small Claims":           "SC                            ",
    "Specialty TX":           "MTX                           ",
    "Summary Process":        "SU                            ",
    "Supplementary Process":  "SP                            ",
    "Veterans Specialty":     "VTX                           ",
}

# Maps CSV display names → <option value> for Party Type (name="ptyCd")
PARTY_TYPE_VALUE_MAP = {
    "All Party Types": " ",
    "Defendant":       "DFNDT                         ",
    "Plaintiff":       "PLNTF                         ",
    "Trustee":         "TRUST                         ",
}

# Maps the display name in the CSV to the <option value> in the Court Department dropdown
DEPT_VALUE_MAP = {
    "BMC":                        "BMC_DEPT  ",
    "District Court":             "DC_DEPT   ",
    "Housing Court":              "HC_DEPT   ",
    "Land Court Department":      "LC_DEPT   ",
    "Probate and Family Court":   "PF_DEPT   ",
    "The Superior Court":         "SC_DEPT   ",
}

def load_search_rows(csv_path=SEARCH_CSV):
    """Load all rows from the search CSV. Returns list of dicts."""
    rows = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Strip whitespace from all values
                rows.append({k: v.strip() for k, v in row.items()})
        log.info(f"[CSV] Loaded {len(rows)} row(s) from {csv_path}")
    except FileNotFoundError:
        log.error(f"[CSV] File not found: {csv_path}")
    return rows


# ============================================================================
# Step 2 — Fill search form (Department → Division → Results per page)
# ============================================================================

def wicket_select(driver, select_el, value_or_text, by_value=False):
    """
    Select an option in a Wicket-enhanced <select>.
    Wicket fires an onchange AJAX call after selection — we wait for any
    pending network activity to settle before returning.
    """
    sel = Select(select_el)
    if by_value:
        sel.select_by_value(value_or_text)
    else:
        sel.select_by_visible_text(value_or_text)
    human_delay(1.5, 2.5)   # let Wicket AJAX update the dependent dropdowns


def convert_date(date_str):
    """
    Convert DD-MM-YYYY (CSV format) → MM/DD/YYYY (site format).
    Falls back to the original string if parsing fails.
    """
    for fmt in ("%d-%m-%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%m/%d/%Y")
        except ValueError:
            pass
    log.warning(f"[DATE] Could not parse date '{date_str}', using as-is.")
    return date_str


def fill_case_type_tab(driver, wait, row):
    """
    Fill the Case Type tab form:
      - Begin Date / End Date  (from CSV FilingDateFrom / FilingDateTo)
      - Case Type              (from CSV CaseType, multi-select)
      - City/Town              → keep "All Cities"
      - Case Status            → keep "All Statuses"
      - Party Type             (from CSV PartyType, multi-select)
    Then click Search.
    """
    begin_raw  = row.get("FilingDateFrom", "").strip()
    end_raw    = row.get("FilingDateTo",   "").strip()
    begin_date = convert_date(begin_raw)
    end_date   = convert_date(end_raw)
    case_type  = row.get("CaseType",   "").strip()
    party_type = row.get("PartyType",  "").strip()

    log.info(f"[TAB] Begin={begin_date}  End={end_date}  "
             f"CaseType='{case_type}'  PartyType='{party_type}'")

    # ── Begin Date ────────────────────────────────────────────────────────────
    try:
        begin_input = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[name='fileDateRange:dateInputBegin']")
            )
        )
        begin_input.clear()
        begin_input.send_keys(begin_date)
        # Trigger onchange so Wicket registers the value
        driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", begin_input)
        human_delay(0.5, 1.0)
        log.info(f"[TAB] Set Begin Date: {begin_date}")
    except Exception as e:
        log.error(f"[TAB] Could not set Begin Date: {e}")
        return False

    # ── End Date ──────────────────────────────────────────────────────────────
    try:
        end_input = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[name='fileDateRange:dateInputEnd']")
            )
        )
        end_input.clear()
        end_input.send_keys(end_date)
        driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", end_input)
        human_delay(0.5, 1.0)
        log.info(f"[TAB] Set End Date: {end_date}")
    except Exception as e:
        log.error(f"[TAB] Could not set End Date: {e}")
        return False

    # ── Case Type (multi-select, name="caseCd") ───────────────────────────────
    case_type_value = CASE_TYPE_VALUE_MAP.get(case_type)
    if not case_type_value:
        log.error(f"[TAB] Unknown CaseType '{case_type}'. "
                  f"Valid: {list(CASE_TYPE_VALUE_MAP.keys())}")
        return False
    try:
        case_type_el = wait.until(
            EC.presence_of_element_located((By.NAME, "caseCd"))
        )
        sel = Select(case_type_el)
        sel.deselect_all()
        sel.select_by_value(case_type_value)
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('change'));", case_type_el
        )
        human_delay(0.5, 1.0)
        log.info(f"[TAB] Selected Case Type: {case_type}")
    except Exception as e:
        log.error(f"[TAB] Could not select Case Type: {e}")
        return False

    # ── City/Town → "All Cities" (value=" ", already default — ensure it) ─────
    try:
        city_el = wait.until(
            EC.presence_of_element_located((By.NAME, "cityCd"))
        )
        sel = Select(city_el)
        sel.deselect_all()
        sel.select_by_value(" ")
        log.info("[TAB] City/Town set to All Cities.")
        human_delay(0.3, 0.6)
    except Exception as e:
        log.warning(f"[TAB] Could not set City/Town (non-fatal): {e}")

    # ── Case Status → "All Statuses" (value=" ", already default — ensure it) ─
    try:
        stat_el = wait.until(
            EC.presence_of_element_located((By.NAME, "statCd"))
        )
        sel = Select(stat_el)
        sel.deselect_all()
        sel.select_by_value(" ")
        log.info("[TAB] Case Status set to All Statuses.")
        human_delay(0.3, 0.6)
    except Exception as e:
        log.warning(f"[TAB] Could not set Case Status (non-fatal): {e}")

    # ── Party Type (multi-select, name="ptyCd") ───────────────────────────────
    party_value = PARTY_TYPE_VALUE_MAP.get(party_type)
    if not party_value:
        log.warning(f"[TAB] Unknown PartyType '{party_type}', defaulting to All Party Types.")
        party_value = " "
    try:
        party_el = wait.until(
            EC.presence_of_element_located((By.NAME, "ptyCd"))
        )
        sel = Select(party_el)
        sel.deselect_all()
        sel.select_by_value(party_value)
        human_delay(0.3, 0.6)
        log.info(f"[TAB] Selected Party Type: {party_type}")
    except Exception as e:
        log.error(f"[TAB] Could not select Party Type: {e}")
        return False

    # ── Click Search ──────────────────────────────────────────────────────────
    try:
        search_btn = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[type='submit'][name='submitLink']")
            )
        )
        human_delay(0.5, 1.0)
        driver.execute_script("arguments[0].click();", search_btn)
        log.info("[TAB] Clicked Search.")
    except Exception as e:
        log.error(f"[TAB] Could not click Search: {e}")
        return False

    # Wait for results page to load (processing dialog disappears)
    try:
        WebDriverWait(driver, 30).until(
            EC.invisibility_of_element_located((By.ID, "processingDialog"))
        )
    except Exception:
        pass
    human_delay(2.0, 3.0)
    log.info(f"[TAB] Search submitted. URL: {driver.current_url}")
    return True
    Fill the search qualifier form for one CSV row:
      1. Select Court Department  (triggers AJAX → reveals Division dropdown)
      2. Select Court Division    (triggers AJAX → reveals Location dropdown)
      3. Set Number of Results to 75

    `row` is a dict with keys: CourtDepartments, CourtDivision, CaseType,
    PartyType, FilingDateFrom, FilingDateTo
    """
    dept_display = row.get("CourtDepartments", "").strip()
    div_display  = row.get("CourtDivision", "").strip()

    log.info(f"[FORM] Department='{dept_display}'  Division='{div_display}'")

    # ── 1. Court Department ──────────────────────────────────────────────────
    dept_value = DEPT_VALUE_MAP.get(dept_display)
    if not dept_value:
        log.error(f"[FORM] Unknown department '{dept_display}'. "
                  f"Valid values: {list(DEPT_VALUE_MAP.keys())}")
        return False

    try:
        dept_select_el = wait.until(
            EC.presence_of_element_located((By.NAME, "sdeptCd"))
        )
        wicket_select(driver, dept_select_el, dept_value, by_value=True)
        log.info(f"[FORM] Selected department: {dept_display}")
    except Exception as e:
        log.error(f"[FORM] Could not select department: {e}")
        return False

    # ── 2. Court Division (appears after AJAX update) ────────────────────────
    try:
        # Wait for the division dropdown to become visible
        div_select_el = WebDriverWait(driver, 15).until(
            EC.visibility_of_element_located((By.NAME, "sdivCd"))
        )
        wicket_select(driver, div_select_el, div_display, by_value=False)
        log.info(f"[FORM] Selected division: {div_display}")
    except Exception as e:
        log.error(f"[FORM] Could not select division '{div_display}': {e}")
        return False

    # ── 3. Number of Results → 75 ────────────────────────────────────────────
    try:
        page_size_el = wait.until(
            EC.presence_of_element_located((By.NAME, "pageSize"))
        )
        wicket_select(driver, page_size_el, "75", by_value=False)
        log.info("[FORM] Set results per page to 75.")
    except Exception as e:
        log.error(f"[FORM] Could not set page size: {e}")
        return False

    # ── 4. Click "Case Type" tab ─────────────────────────────────────────────
    try:
        case_type_tab = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH,
                 "//ul/li/a[.//span[normalize-space(text())='Case Type']]")
            )
        )
        driver.execute_script("arguments[0].click();", case_type_tab)
        log.info("[FORM] Clicked 'Case Type' tab.")

        # Wait for the tab panel to update — the tab li gets class 'selected'
        WebDriverWait(driver, 15).until(
            lambda d: "selected" in (
                d.find_element(
                    By.XPATH,
                    "//ul/li[.//span[normalize-space(text())='Case Type']]"
                ).get_attribute("class") or ""
            )
        )
        human_delay(1.0, 2.0)
        log.info("[FORM] 'Case Type' tab is now active.")
    except Exception as e:
        log.error(f"[FORM] Could not click 'Case Type' tab: {e}")
        return False

    # ── 5. Fill Case Type tab form and submit ────────────────────────────────
    return fill_case_type_tab(driver, wait, row)


# ============================================================================
# Main
# ============================================================================

def main():
    setup_logger()
    log.info("=" * 60)
    log.info("MA Trial Court — Search by Case Type Crawler")
    log.info("=" * 60)

    search_rows = load_search_rows(SEARCH_CSV)
    if not search_rows:
        log.error(f"No rows found in {SEARCH_CSV}. Exiting.")
        return

    options = webdriver.ChromeOptions()
    # Uncomment below to run headless (no browser window):
    # options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    driver.maximize_window()
    wait = WebDriverWait(driver, 20)

    try:
        # ── Step 1: welcome page → captcha → click "Click Here" ──────────────
        success = open_and_enter_site(driver, wait)
        if not success:
            log.error("[FAIL] Could not get past the welcome page.")
            return

        log.info("[DONE] Step 1 complete — now on search page.")
        log.info(f"[URL]  {driver.current_url}")

        # ── Step 2+3: for each CSV row, fill form + collect results ──────────
        for i, row in enumerate(search_rows):
            dept   = row.get("CourtDepartments", "")
            div    = row.get("CourtDivision", "")
            begin  = convert_date(row.get("FilingDateFrom", ""))
            end    = convert_date(row.get("FilingDateTo", ""))
            log.info(f"--- [{i+1}/{len(search_rows)}] {dept} / {div} "
                     f"{begin} → {end} ---")

            search_meta = {
                "CourtDepartment":  dept,
                "CourtDivision":    div,
                "SearchBeginDate":  begin,
                "SearchEndDate":    end,
            }

            all_rows = collect_with_smart_split(driver, wait, row, begin, end, depth=0)
            log.info(f"[DONE] Row {i+1}: collected {len(all_rows)} total records.")
            append_results_to_csv(all_rows, search_meta)
            human_delay(2.0, 3.0)

    except Exception as e:
        log.error(f"[FATAL] {e}")
    finally:
        input("\nPress Enter to close the browser...")
        driver.quit()


if __name__ == "__main__":
    main()
