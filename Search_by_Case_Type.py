"""
Massachusetts Trial Court Case Access — Search by Case Type
Flow (incremental build):
  Step 1: Open home page → solve reCAPTCHA → click "Click Here" to enter search
  Step 2: (coming) Fill search form by case type / date / court
  Step 3: (coming) Paginate results and extract case data
"""

import time
import random
import base64
import io
import os
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
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
# Main
# ============================================================================

def main():
    setup_logger()
    log.info("=" * 60)
    log.info("MA Trial Court — Search by Case Type Crawler")
    log.info("=" * 60)

    options = webdriver.ChromeOptions()
    # Uncomment below to run headless (no browser window):
    # options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    driver.maximize_window()
    wait = WebDriverWait(driver, 20)

    try:
        success = open_and_enter_site(driver, wait)
        if success:
            log.info("[DONE] Step 1 complete — now on search page.")
            log.info(f"[URL]  {driver.current_url}")
            # Step 2 (search form fill) will be added here next
        else:
            log.error("[FAIL] Could not get past the welcome page.")
    except Exception as e:
        log.error(f"[FATAL] {e}")
    finally:
        input("\nPress Enter to close the browser...")
        driver.quit()


if __name__ == "__main__":
    main()
