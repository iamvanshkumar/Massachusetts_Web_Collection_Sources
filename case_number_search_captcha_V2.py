"""
CA San Bernardino County — Case Number Search (with reCAPTCHA + Data Extraction)
Flow:
  1. Load case numbers from cases.csv
  2. Navigate to /search, fill case number, solve reCAPTCHA with Gemini
  3. On the case detail page:
     - Click CHARGES/DISPO tab to render charge data
     - Extract case-level fields, parties, and per-charge rows
  4. Write one CSV row per charge to case_details.csv
     Write parties to parties.csv
"""

import time
import random
import base64
import io
import os
import csv
import re
import logging
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path

# Load .env from this folder, or fall back to the workspace root
load_dotenv(dotenv_path=Path(__file__).parent / ".env")
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from PIL import Image
from bs4 import BeautifulSoup, NavigableString
from google import genai
from google.genai import types

# ============================================================================
# Configuration
# ============================================================================

HOME_URL   = "https://cap.sb-court.org/"
SEARCH_URL = "https://cap.sb-court.org/search"
CASES_CSV  = "cases.csv"
OUTPUT_DIR = "cases_output"
LOG_DIR    = "logs"

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable not set")

EMAIL    = os.getenv("SB_EMAIL")
PASSWORD = os.getenv("SB_PASSWORD")

if not EMAIL or not PASSWORD:
    raise ValueError("SB_EMAIL and SB_PASSWORD must be set in .env")

genai_client = genai.Client(api_key=API_KEY)
GEMINI_MODEL = "gemini-2.5-flash"

# ============================================================================
# Logger Setup
# ============================================================================

def setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    fmt     = "%(asctime)s  %(levelname)-8s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(
        level=logging.INFO, format=fmt, datefmt=datefmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.info(f"Log file: {log_file}")
    return log_file

log = logging.getLogger(__name__)

# ============================================================================
# Helpers — browser
# ============================================================================

def human_delay(min_sec=0.8, max_sec=2.2):
    time.sleep(random.uniform(min_sec, max_sec))

def human_move_and_click(driver, element):
    ActionChains(driver)\
        .move_to_element(element)\
        .pause(random.uniform(0.3, 0.7))\
        .click()\
        .perform()

def type_like_human(element, text):
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.07, 0.18))

def wait_for_angular(driver, timeout=15):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script(
                "return (window.angular !== undefined) && "
                "(angular.element(document.body).injector() !== undefined) && "
                "(!angular.element(document.body).injector().get('$http').pendingRequests.length);"
            )
        )
    except Exception:
        pass

# ============================================================================
# Helpers — text / parsing
# ============================================================================

def clean_text(value):
    """Normalize whitespace for CSV output."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def split_detail_type_description(value):
    """
    Split charge detail string like 'Infraction - VC24600(A)-I: Taillamps'.
    Returns (ChargeLevel, Statute, StatuteDescription).
      ChargeLevel        = text before first ' - '
      Statute            = text between first ' - ' and first ':'
      StatuteDescription = text after first ':'
    Falls back to empty string if delimiter is missing.
    """
    value = clean_text(value)
    if not value:
        return "", "", ""
    if " - " in value:
        charge_level, remainder = value.split(" - ", 1)
    else:
        charge_level, remainder = "", value
    if ":" in remainder:
        statute, statute_description = remainder.split(":", 1)
    else:
        statute, statute_description = remainder, ""
    return clean_text(charge_level), clean_text(statute), clean_text(statute_description)


def append_unique(target, key, value):
    value = clean_text(value)
    if not value:
        return
    existing = [p.strip() for p in target.get(key, "").split(";") if p.strip()]
    if value not in existing:
        existing.append(value)
    target[key] = "; ".join(existing)


def get_labeled_value(container, label):
    """Read a value that follows a <strong>/<b> label up to the next <br>.
    Skips elements that are hidden via ng-hide (e.g. Warrant status shown only
    to users with Elevated Access — we leave CaseStatus blank in that case).
    """
    label_re = re.compile(rf"^{re.escape(label)}\s*:?\s*$", re.I)
    label_tag = container.find(
        ["strong", "b"],
        string=lambda text: text and label_re.match(clean_text(text))
    )
    if not label_tag:
        return ""
    values = []
    for sibling in label_tag.next_siblings:
        if getattr(sibling, "name", None) == "br":
            break
        # Skip elements hidden by ng-hide — their text is in the DOM but not
        # visible to the user (e.g. Warrant status requires Elevated Access).
        if hasattr(sibling, "get") and "ng-hide" in sibling.get("class", []):
            continue
        values.append(
            sibling.get_text(" ", strip=True)
            if hasattr(sibling, "get_text") else str(sibling)
        )
    return clean_text(" ".join(values))


def _parse_date_safe(value):
    """Try common date formats; return datetime or datetime.min on failure."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return datetime.min


# ============================================================================
# Extraction — Charges / Dispositions / Sentences
# ============================================================================

def extract_charges_and_dispositions(soup):
    """
    Stateful extraction of the CHARGES/DISPO table.

    Actual DOM structure (confirmed from live HTML):

    <tbody ng-repeat="chrg in vm.case.caseCharges">
      <tr>  ← CHARGE row  (td[1] contains <b class="charge-text">)
      <tr>  ← PLEA row    (ignored)
      <tr>  ← DISPOSITION row (td[1] contains <b class="disposition-text">)
      <tr ng-repeat-start="sen in chrg.sentences">  ← SENTENCE row
      <tr ng-repeat="(groupName, caption) in sen.sentenceComponents | groupBy:'groupName'">
            ← COMPONENT GROUP row — 4 cells:
              td[0] = empty
              td[1] = <b class="components-text">Confinement and Detention</b>
              td[2] = nested <table> with <tr class="components-tr"> for each LABEL (compCap)
              td[3] = nested <table> with <tr class="components-tr"> for each VALUE (compVal)
            Labels and values are positionally aligned (index-to-index).

    Field mapping:
      groupName "Confinement and Detention":
        label "Adult Confinement Type" → SentenceType        e.g. "County Jail"
        label "Term Days"              → SentenceDescription e.g. "8 Days"
        label "Term Months"            → SentenceDescription (fallback)
        label "Term Years"             → SentenceDescription (fallback)
      groupName "Supervision":
        label "Type"                   → ProbationType        e.g. "Summary Probation (Court)"
        label "Years"                  → ProbationDescription e.g. "1 Years"
        label "Months"                 → ProbationDescription (fallback)

    Rules:
    - CountNumber only from CHARGE row; leading zeros preserved, prefixed with ' for Excel
    - Multiple dispositions → keep latest by date only
    - Multiple sentences   → keep latest by date only
    - PLEA rows are ignored; nothing mixed across counts
    - Charges sorted in strict ascending CountNumber order before return
    """
    charges_list = []
    charges_div  = soup.find("div", id="charges")
    if not charges_div:
        return charges_list

    heading = charges_div.find("h3", string=lambda t: t and "CHARGES" in t.upper())
    table   = heading.find_next("table") if heading else charges_div.find("table")
    if not table:
        return charges_list

    def _extract_group_pairs(group_row):
        """
        Given a components-text <tr>, extract the label→value dict.

        td[2] = nested table of label rows (compCap) — text inside <b> tags
        td[3] = nested table of value rows (compVal) — plain text

        Returns {label_lower: value_text}
        """
        cells = group_row.find_all("td", recursive=False)
        if len(cells) < 4:
            return {}

        # Labels: td[2] nested table, each components-tr row, text of <b>
        label_trs = cells[2].find_all("tr", class_="components-tr")
        labels = [clean_text(tr.get_text(" ", strip=True)) for tr in label_trs]

        # Values: td[3] nested table, each components-tr row, plain text
        value_trs = cells[3].find_all("tr", class_="components-tr")
        values = [clean_text(tr.get_text(" ", strip=True)) for tr in value_trs]

        return {lbl.lower(): val for lbl, val in zip(labels, values) if lbl}

    for tbody in table.find_all("tbody", attrs={"ng-repeat": lambda v: v and "caseCharges" in v}):
        rows = tbody.find_all("tr", recursive=False)
        if not rows:
            continue

        charge           = {}
        dispositions     = []
        sentences        = []
        current_sentence = None

        for row in rows:
            row_html = str(row)

            is_charge      = "charge-text"      in row_html
            is_disposition = "disposition-text" in row_html
            is_sentence    = "sentence-text"    in row_html
            is_component   = "components-text"  in row_html

            cells = row.find_all("td", recursive=False)

            # ── CHARGE ──────────────────────────────────────────────────────
            if is_charge and len(cells) >= 4:
                raw_count   = clean_text(cells[0].get_text(" ", strip=True))
                row_date    = clean_text(cells[2].get_text(" ", strip=True))
                detail_text = clean_text(cells[3].get_text(" ", strip=True))
                charge_level, statute, statute_description = split_detail_type_description(detail_text)
                charge["CountNumber"]        = f"'{raw_count}" if raw_count else ""
                charge["OffenseDate"]        = row_date
                charge["ChargeLevel"]        = charge_level
                charge["Statute"]            = statute
                charge["StatuteDescription"] = statute_description
                current_sentence = None

            # ── DISPOSITION ─────────────────────────────────────────────────
            elif is_disposition and len(cells) >= 4:
                row_date    = clean_text(cells[2].get_text(" ", strip=True))
                detail_text = clean_text(cells[3].get_text(" ", strip=True))
                dispositions.append({
                    "date":     _parse_date_safe(row_date),
                    "raw_date": row_date,
                    "detail":   detail_text,
                })

            # ── SENTENCE ────────────────────────────────────────────────────
            elif is_sentence and len(cells) >= 4:
                row_date = clean_text(cells[2].get_text(" ", strip=True))
                current_sentence = {
                    "date":   _parse_date_safe(row_date),
                    "groups": {},   # groupName → {label_lower: value}
                }
                sentences.append(current_sentence)

            # ── COMPONENT GROUP row ──────────────────────────────────────────
            # td[1] holds <b class="components-text">GroupName</b>
            # td[2] holds nested label table, td[3] holds nested value table
            elif is_component and current_sentence is not None and len(cells) >= 4:
                b_tag      = cells[1].find("b", class_="components-text")
                group_name = clean_text(b_tag.get_text()) if b_tag else ""
                pairs      = _extract_group_pairs(row)
                if group_name and pairs:
                    current_sentence["groups"][group_name] = pairs

            # ── PLEA — intentionally ignored ────────────────────────────────

        # ── Resolve latest disposition ──────────────────────────────────────
        if dispositions:
            latest_dispo = max(dispositions, key=lambda x: x["date"])
            charge["DispositionDate"]   = latest_dispo["raw_date"]
            charge["DispositionDetail"] = latest_dispo["detail"]
        else:
            charge["DispositionDate"]   = ""
            charge["DispositionDetail"] = ""

        # ── Resolve latest sentence ──────────────────────────────────────────
        if sentences:
            latest_sent = max(sentences, key=lambda x: x["date"])
            groups      = latest_sent["groups"]

            # Confinement and Detention → Sentence fields
            conf = groups.get("Confinement and Detention", {})
            charge["SentenceType"] = conf.get("adult confinement type", "")
            charge["SentenceDescription"] = (
                conf.get("term days")
                or conf.get("term months")
                or conf.get("term years")
                or ""
            )

            # Supervision → Probation fields
            sup = groups.get("Supervision", {})
            charge["ProbationType"] = sup.get("type", "")
            charge["ProbationDescription"] = (
                sup.get("years")
                or sup.get("months")
                or ""
            )
        else:
            charge["SentenceType"]        = ""
            charge["SentenceDescription"] = ""
            charge["ProbationType"]        = ""
            charge["ProbationDescription"] = ""

        if charge:
            charges_list.append(charge)

    # Sort charges in strict ascending CountNumber order ('001, '002, '003 …).
    # Strip the leading Excel-escape apostrophe before comparing numerically;
    # fall back to string sort for any non-numeric values.
    def _count_sort_key(c):
        raw = c.get("CountNumber", "").lstrip("'").strip()
        try:
            return (0, int(raw))
        except ValueError:
            return (1, raw)

    charges_list.sort(key=_count_sort_key)
    return charges_list


# ============================================================================
# Extraction — Case Details (case-level + parties)
# ============================================================================

def extract_case_details(html_content):
    """
    Extract case details from the HTML content of the case detail page.
    Returns:
      case_details  – dict of case-level fields
      charges_list  – list of per-charge dicts (one entry per COUNT)
      parties       – list of party dicts
    """
    soup    = BeautifulSoup(html_content, "html.parser")
    details = {}
    parties = []

    # ── Case info section (div#caseinfo1) ────────────────────────────────────
    case_info_div = soup.find("div", id="caseinfo1")
    if case_info_div:
        label_map = {
            "Case Type":       "CourtCaseType",
            "Case Number":     "CaseNumber",
            "Citation Number": "CitationNumber",
            "Filing Date":     "FilingDate",
            "Case Status":     "CaseStatus",
            "Court Location":  "CourtName",
            "Judicial Officer":"JudgeName",
        }
        for label, field in label_map.items():
            value = get_labeled_value(case_info_div, label)
            if value:
                details[field] = value

        # Fallback: parse <p> children as "Key: Value" lines
        case_p = case_info_div.find("p")
        if case_p:
            case_lines = []
            for content in case_p.children:
                if isinstance(content, str):
                    text = content.strip()
                    if text:
                        case_lines.append(text)
                elif content.name == "br":
                    continue
                else:
                    # Skip elements hidden by ng-hide (e.g. Warrant span)
                    if "ng-hide" in content.get("class", []):
                        continue
                    text = content.get_text(strip=True)
                    if text:
                        case_lines.append(text)

            key_map = {
                "Case Type":        "CourtCaseType",
                "Case Number":      "CaseNumber",
                "Citation Number":  "CitationNumber",
                "Filing Date":      "FilingDate",
                "Case Status":      "CaseStatus",
                "Court Location":   "CourtName",
                "Judicial Officer": "JudgeName",
            }
            for line in case_lines:
                line = line.strip()
                if ":" in line:
                    key, _, value = line.partition(":")
                    key   = key.strip()
                    value = value.strip()
                    if value and key in key_map:
                        details.setdefault(key_map[key], value)

        # ── Alias — primary: ng-repeat spans ────────────────────────────────
        alias_h3 = case_info_div.find("h3", string=lambda t: t and "Alias" in t)
        if alias_h3:
            alias_p = alias_h3.find_next("p")
            if alias_p:
                alias_spans = alias_p.find_all(
                    "span", {"ng-repeat": lambda x: x and "alias in" in x}
                )
                aliases = [s.get_text(strip=True) for s in alias_spans if s.get_text(strip=True)]
                if aliases:
                    details["Alias"] = "; ".join(aliases)

    # ── Parties table (div#parties) ──────────────────────────────────────────
    parties_div = soup.find("div", id="parties")
    if parties_div:
        table = parties_div.find("table", id="DataTables_Table_0") \
                or parties_div.find("table")
        if table:
            tbody = table.find("tbody")
            if tbody:
                for row in tbody.find_all("tr"):
                    tds = row.find_all("td")
                    if len(tds) < 3:
                        continue
                    party_type   = tds[0].get_text(strip=True)
                    party_name   = tds[1].get_text(separator=" ", strip=True)
                    party_status = tds[2].get_text(strip=True)
                    parties.append({
                        "type":   party_type,
                        "name":   party_name,
                        "status": party_status,
                    })

                    if party_type.lower() == "defendant":
                        # NameRaw: first direct text node (before <br>/<i> aliases)
                        if "NameRaw" not in details:
                            first_text = next(
                                (str(c) for c in tds[1].children
                                 if isinstance(c, NavigableString) and c.strip()),
                                party_name
                            )
                            details["NameRaw"] = clean_text(first_text)

                        # Alias fallback: <i> tags in the Defendant cell
                        if "Alias" not in details:
                            alias_tags = tds[1].find_all("i")
                            aliases = [
                                clean_text(i.get_text())
                                for i in alias_tags
                                if i.get_text(strip=True)
                            ]
                            if aliases:
                                details["Alias"] = "; ".join(aliases)

    # ── Charges (one dict per COUNT) ─────────────────────────────────────────
    charges_list = extract_charges_and_dispositions(soup)

    return details, charges_list, parties


# ============================================================================
# CSV Writers
# ============================================================================

def append_case_details(case_details, charges_list):
    """
    Write one CSV row per charge, with case-level fields repeated on every row.
    Falls back to one row with case-level data only if no charges found.
    """
    if not case_details and not charges_list:
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath     = os.path.join(OUTPUT_DIR, "case_details.csv")
    write_header = not os.path.exists(filepath)

    case_fields   = ["CourtCaseType", "CaseNumber", "CitationNumber", "FilingDate",
                     "CaseStatus", "CourtName", "JudgeName", "Alias", "NameRaw"]
    charge_fields = ["CountNumber", "OffenseDate", "ChargeLevel", "Statute",
                     "StatuteDescription", "DispositionDate", "DispositionDetail",
                     "SentenceType", "SentenceDescription", "ProbationType", "ProbationDescription"]
    fieldnames = case_fields + charge_fields

    with open(filepath, "a", newline="", encoding="utf-8", buffering=1) as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        if write_header:
            writer.writeheader()
            f.flush()
            os.fsync(f.fileno())

        if charges_list:
            for charge in charges_list:
                writer.writerow({**case_details, **charge})
                f.flush()
                os.fsync(f.fileno())
        else:
            writer.writerow(case_details)
            f.flush()
            os.fsync(f.fileno())

    case_num = case_details.get("CaseNumber", "?")
    log.info(f"[SAVE] {len(charges_list) or 1} charge row(s) for {case_num} → {filepath}")


def append_parties(case_number, parties):
    """Append extracted parties to parties.csv."""
    if not parties:
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath     = os.path.join(OUTPUT_DIR, "parties.csv")
    write_header = not os.path.exists(filepath)
    fieldnames   = ["case_number", "party_type", "party_name", "party_status"]

    with open(filepath, "a", newline="", encoding="utf-8", buffering=1) as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
            f.flush()
            os.fsync(f.fileno())

        for party in parties:
            writer.writerow({
                "case_number":  case_number,
                "party_type":   party["type"],
                "party_name":   party["name"],
                "party_status": party["status"],
            })
            f.flush()
            os.fsync(f.fileno())

    log.info(f"[SAVE] {len(parties)} party row(s) for {case_number} → {filepath}")


# ============================================================================
# reCAPTCHA — Checkbox
# ============================================================================

def wait_for_recaptcha_iframe(driver, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[title='reCAPTCHA']")
            for iframe in iframes:
                src = iframe.get_attribute("src") or ""
                if "recaptcha" in src and iframe.is_displayed():
                    return iframe
        except Exception:
            pass
        time.sleep(0.5)
    return None


def click_recaptcha_checkbox(driver, iframe):
    try:
        driver.switch_to.frame(iframe)
        checkbox = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "recaptcha-anchor"))
        )
        human_delay(0.8, 1.5)
        human_move_and_click(driver, checkbox)
        log.info("[CAPTCHA] Clicked checkbox.")
        driver.switch_to.default_content()
        return True
    except Exception as e:
        log.error(f"[ERROR] Checkbox click failed: {e}")
        driver.switch_to.default_content()
        return False


def is_checkbox_solved(driver):
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
        grid_size = len(tiles) if tiles else 16
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
# CSV Loader
# ============================================================================

def load_case_numbers(csv_path):
    """Load case numbers from the first column of a CSV file."""
    case_numbers = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if row and row[0].strip():
                    case_numbers.append(row[0].strip())
        log.info(f"[CSV] Loaded {len(case_numbers)} case numbers from {csv_path}")
    except FileNotFoundError:
        log.error(f"[ERROR] CSV file not found: {csv_path}")
    return case_numbers


# ============================================================================
# Navigation
# ============================================================================

def navigate_and_login(driver, wait):
    """Homepage → LOGIN/REGISTER → fill credentials → dashboard."""
    log.info("[NAV] Loading homepage...")
    driver.get(HOME_URL)
    wait_for_angular(driver, timeout=15)
    human_delay(2.0, 3.0)

    login_link = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[@href='/login']")))
    human_move_and_click(driver, login_link)
    log.info("[NAV] Clicked LOGIN/REGISTER.")

    wait.until(EC.url_contains("/login"))
    wait_for_angular(driver)
    human_delay(1.5, 2.5)

    # Email
    email_field = wait.until(EC.presence_of_element_located((By.ID, "email")))
    human_move_and_click(driver, email_field)
    type_like_human(email_field, EMAIL)
    email_field.send_keys(Keys.TAB)
    human_delay(0.5, 1.0)

    # Password
    pwd_field = driver.find_element(By.ID, "password")
    human_move_and_click(driver, pwd_field)
    type_like_human(pwd_field, PASSWORD)
    human_delay(0.8, 1.5)

    # Log In button
    login_btn = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, "//button[@type='submit' and .//span[text()='Log In']]")
        )
    )
    human_move_and_click(driver, login_btn)
    log.info("[LOGIN] Submitted, waiting for dashboard...")

    WebDriverWait(driver, 30).until(EC.none_of(EC.url_contains("/login")))
    log.info(f"[LOGIN] Success. URL: {driver.current_url}")
    human_delay(2.0, 3.0)
    return True


def navigate_to_search(driver, wait):
    """Homepage → SEARCH dropdown → CASE INFORMATION → /search"""
    try:
        log.info("[NAV] Loading homepage...")
        driver.get(HOME_URL)
        wait_for_angular(driver, timeout=15)
        human_delay(2.0, 3.0)

        search_dropdown = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//a[@class='dropdown-toggle' and .//i[contains(@class,'md-search')]]")
            )
        )
        human_delay(0.5, 1.0)
        human_move_and_click(driver, search_dropdown)
        log.info("[NAV] Clicked SEARCH dropdown.")
        human_delay(0.5, 1.0)

        case_info_link = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//a[@href='/search' and text()='CASE INFORMATION']")
            )
        )
        human_move_and_click(driver, case_info_link)
        log.info("[NAV] Clicked CASE INFORMATION.")

        wait.until(EC.url_contains("/search"))
        wait_for_angular(driver, timeout=15)
        human_delay(2.0, 3.0)
        log.info(f"[NAV] On search page: {driver.current_url}")
        return True

    except Exception as e:
        log.error(f"[ERROR] navigate_to_search failed: {e}")
        return False


# ============================================================================
# Search, Extract & Save
# ============================================================================

def search_case(driver, wait, case_number, first_run=False):
    """Submit the case number search form. Returns True on success."""
    try:
        log.info(f"[SEARCH] Starting search for: {case_number}")

        if first_run:
            if not navigate_to_search(driver, wait):
                return False
        else:
            log.info(f"[NAV] Navigating directly to {SEARCH_URL}")
            driver.get(SEARCH_URL)
            wait_for_angular(driver, timeout=15)
            human_delay(2.0, 3.0)

        case_input = wait.until(EC.presence_of_element_located((By.ID, "caseNumber")))
        human_delay(0.5, 1.0)
        human_move_and_click(driver, case_input)
        case_input.clear()
        type_like_human(case_input, case_number)
        log.info(f"[SEARCH] Typed: {case_number}")

        case_input.send_keys(Keys.TAB)
        human_delay(1.0, 2.0)

        if not handle_recaptcha(driver):
            log.error("[ERROR] reCAPTCHA not solved.")
            return False

        human_delay(1.0, 2.0)

        log.info("[SEARCH] Waiting for Search button...")
        search_btn = wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[@ng-click=\"vm.searchByCaseNumber(vm.caseForm.caseNumber)\"]")
            )
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if not driver.execute_script("return arguments[0].disabled;", search_btn):
                break
            time.sleep(0.5)
        else:
            log.error("[ERROR] Search button never enabled.")
            return False

        human_delay(0.5, 1.0)
        driver.execute_script("arguments[0].click();", search_btn)
        log.info("[SEARCH] Submitted.")
        human_delay(3.0, 5.0)
        return True

    except Exception as e:
        log.error(f"[ERROR] search_case failed: {e}")
        return False


def extract_and_save(driver, wait, case_number):
    """
    On the case detail page:
      1. Click CHARGES/DISPO tab so Angular renders charge rows
      2. Save raw HTML
      3. Extract case details, charges, parties
      4. Write to CSV files
    """
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
        )
        wait_for_angular(driver, timeout=10)
        human_delay(1.0, 2.0)

        # Click CHARGES/DISPO tab to ensure Angular renders charge data
        try:
            charges_tab = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "tab-charges"))
            )
            driver.execute_script("arguments[0].click();", charges_tab)
            log.info(f"[EXTRACT] Clicked CHARGES/DISPO tab for {case_number}")
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-scroll table"))
            )
            wait_for_angular(driver, timeout=10)
            human_delay(1.0, 1.5)
        except Exception as e:
            log.warning(f"[EXTRACT] CHARGES tab not available for {case_number}: {e}")

        # Save raw HTML
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in case_number)
        html_path = os.path.join(OUTPUT_DIR, f"{safe_name}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log.info(f"[SAVE] HTML → {html_path}")

        # Extract and write CSVs
        case_details, charges_list, parties = extract_case_details(driver.page_source)
        append_case_details(case_details, charges_list)
        append_parties(case_number, parties)

    except Exception as e:
        log.error(f"[EXTRACT] Failed for {case_number}: {e}")


# ============================================================================
# Main
# ============================================================================

def main():
    setup_logger()
    log.info("=" * 60)
    log.info("CA San Bernardino County — Case Number Search Crawler")
    log.info("=" * 60)

    case_numbers = load_case_numbers(CASES_CSV)
    if not case_numbers:
        log.error(f"No case numbers found in {CASES_CSV}. Exiting.")
        return

    options = webdriver.ChromeOptions()
    driver  = webdriver.Chrome(options=options)
    driver.maximize_window()
    wait = WebDriverWait(driver, 20)

    results       = []   # (case_number, status, elapsed_sec)
    session_start = time.time()

    try:
        navigate_and_login(driver, wait)

        for i, case_number in enumerate(case_numbers):
            first_run  = (i == 0)
            case_start = time.time()
            log.info(f"--- [{i+1}/{len(case_numbers)}] Processing: {case_number} ---")

            success = search_case(driver, wait, case_number, first_run=first_run)
            if success:
                extract_and_save(driver, wait, case_number)
                status = "OK"
            else:
                log.warning(f"[SKIP] {case_number}")
                status = "SKIP"

            elapsed = time.time() - case_start
            results.append((case_number, status, elapsed))
            log.info(f"[TIMING] {case_number} → {status} in {elapsed:.1f}s")
            human_delay(2.0, 4.0)

    except Exception as e:
        log.error(f"[FATAL] {e}")

    finally:
        session_elapsed = time.time() - session_start

        log.info("")
        log.info("=" * 60)
        log.info("SUMMARY")
        log.info("=" * 60)
        log.info(f"{'Case Number':<25} {'Status':<8} {'Time (s)':>10}")
        log.info("-" * 45)
        for cn, st, el in results:
            log.info(f"{cn:<25} {st:<8} {el:>10.1f}")
        log.info("-" * 45)
        ok_count = sum(1 for _, s, _ in results if s == "OK")
        avg      = (sum(e for _, _, e in results) / len(results)) if results else 0
        log.info(f"Total: {len(results)} | OK: {ok_count} | Skipped: {len(results)-ok_count}")
        log.info(f"Session time: {session_elapsed:.1f}s | Avg per case: {avg:.1f}s")
        log.info("=" * 60)

        input("\nPress Enter to close the browser...")
        driver.quit()


if __name__ == "__main__":
    main()
