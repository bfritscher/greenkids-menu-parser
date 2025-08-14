import requests
import json
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.id import ID
import traceback
from pypdf import PdfReader
import re
import os
import dateparser
import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin

DATA_FOLDER = 'data'
PDF_FOLDER = os.path.join(DATA_FOLDER, 'pdf')
os.makedirs(DATA_FOLDER, exist_ok=True)
os.makedirs(PDF_FOLDER, exist_ok=True)

# Add a session with retries and browser-like headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
SESSION = requests.Session()
_retries = Retry(
    total=5,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504, 509, 510],
    allowed_methods=["GET", "HEAD", "OPTIONS"],
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retries)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)


def find_link():
    url = "https://cver.ch/?page_id=1467"
    response = SESSION.get(url, headers=HEADERS)
    response.raise_for_status()
    pdf_links = re.findall(r'="([^"]+\.pdf)"', response.text, re.IGNORECASE)
    links = pdf_links
    print(f"Found PDF link: {links}")
    return links

def url_to_file_path(url):
    filename = url.split("/")[-1]
    return os.path.join(PDF_FOLDER, filename)

def download_link(url):
    response = SESSION.get(url, headers=HEADERS)
    response.raise_for_status()
    path = url_to_file_path(url)
    with open(path, "wb") as f:
        f.write(response.content)
    return path


def extract_menus(text):
    """
    Parse new CVER text format based on a week range and weekday blocks.

    Rules:
    - Compute dates from line like: "Semaine du 04.08.2025 au 08.08.2025".
    - Find blocks starting with weekday name (lundi..vendredi), ending at next weekday or EOF.
    - Ignore legend/footnote lines and known boilerplate.
    - Trim excessive whitespace in descriptions.
    """

    def _normalize_text(s: str) -> str:
        # Normalize line endings and spaces, keep newlines
        s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\u00A0", " ")
        # Collapse sequences of more than 2 blank lines to a single blank line
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s

    def _remove_noise_lines(s: str) -> str:
        """Remove legend/boilerplate lines that must be ignored."""
        ignore_starts = (
            "rouge :",  # Rouge : porc
            "jaune :",  # Jaune : contient du Gluten
            "bleu :",   # Bleu : contient du lactose
            "brun :",   # Brun : fruits à coques
            "vert :",   # Vert : soja
            "provenance :",
            "poulet, ",   # Poulet, bœuf, veau, porc : Suisse
            "poisson :",
            "les menus de laure et françois",
            # keep "semaine du" also as substring filter below
        )
        ignore_substrings = ("semaine du",)
        out_lines = []
        for line in s.splitlines():
            ls = line.strip()
            lsl = ls.lower()
            if not ls:
                out_lines.append("")
                continue
            if any(lsl.startswith(prefix) for prefix in ignore_starts):
                continue
            if any(sub in lsl for sub in ignore_substrings):
                continue
            out_lines.append(line)
        return "\n".join(out_lines)

    def _parse_week_range(s: str):
        """Return (start_date, end_date) as datetime.date or (None, None)."""
        m = re.search(
            r"Semaine\s+du\s+(\d{1,2}[./]\d{1,2}[./]\d{4})\s+au\s+(\d{1,2}[./]\d{1,2}[./]\d{4})",
            s,
            re.IGNORECASE,
        )
        if not m:
            return (None, None)
        sd_str, ed_str = m.group(1), m.group(2)
        # Use dateparser with DMY order
        sd = dateparser.parse(sd_str, languages=["fr"], settings={"DATE_ORDER": "DMY"})
        ed = dateparser.parse(ed_str, languages=["fr"], settings={"DATE_ORDER": "DMY"})
        if sd:
            sd = sd.date()
        if ed:
            ed = ed.date()
        return (sd, ed)

    def _tidy_block(s: str) -> str:
        # Strip day name already removed; clean multiple spaces and blank lines
        # Remove leftover leading/trailing whitespace
        s = s.strip()
        # Collapse runs of whitespace within lines to single spaces
        s = "\n".join(re.sub(r"\s+", " ", ln).strip() for ln in s.splitlines())
        # Remove duplicate consecutive identical lines (rare from PDFs)
        cleaned_lines = []
        prev = None
        for ln in s.splitlines():
            if ln != prev:
                cleaned_lines.append(ln)
            prev = ln
        # Collapse multiple blank lines after cleaning
        t = "\n".join(cleaned_lines)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()

    text = _normalize_text(text)

    # Parse the week range BEFORE removing noise so we don't lose the date
    start_date, end_date = _parse_week_range(text)

    # Now remove boilerplate (including the week header) so it won't be in blocks
    text = _remove_noise_lines(text)

    # Map weekdays to dates based on the start of the week
    days_order = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    day_to_offset = {d: i for i, d in enumerate(days_order)}
    day_to_date = {}
    if start_date:
        for d, offset in day_to_offset.items():
            day_to_date[d] = start_date + datetime.timedelta(days=offset)

    # Find blocks per day
    day_header_re = re.compile(r"(?mi)^\s*(lundi|mardi|mercredi|jeudi|vendredi)\b.*$")
    matches = list(day_header_re.finditer(text))

    menus = []
    for idx, m in enumerate(matches):
        day_lc = m.group(1).lower()
        start_pos = m.start()
        end_pos = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start_pos:end_pos]
        # Remove the leading day name from the block
        block = re.sub(r"(?is)^\s*(lundi|mardi|mercredi|jeudi|vendredi)\s*", "", block, count=1)
        description = _tidy_block(block)

        menus.append(
            {
                "dow": day_lc.capitalize(),
                "date": day_to_date.get(day_lc),
                "description": description,
            }
        )

    return menus

def get_menus():
    menus = []
    for link in find_link():
        file_path = download_link(link)
        reader = PdfReader(file_path)
        page = reader.pages[0]
        text = page.extract_text()
        menus.extend(extract_menus(text))
    return menus


def main(context):
    client = Client()

    databases = Databases(client)

    if not os.environ.get('APPWRITE_FUNCTION_API_ENDPOINT') or not context.req.headers["x-appwrite-key"]:
        raise Exception(
            'Environment variables are not set. Function cannot use Appwrite SDK.')
    (client
     .set_endpoint(os.environ.get('APPWRITE_FUNCTION_API_ENDPOINT', None))
     .set_project(os.environ.get('APPWRITE_FUNCTION_PROJECT_ID', None))
     .set_key(context.req.headers["x-appwrite-key"])
     )

    def save_menu(menu):
        return databases.create_document('cver', 'menu', ID.unique(), json.dumps(menu, default=str))

    new_found = 0
    menus = get_menus()
    for menu in menus:
        try:
            context.log(save_menu(menu))
            new_found += 1
        except Exception as e:
            error_message = traceback.format_exc()
            context.error(error_message)

    if new_found == 0:
        context.error('No new menus found')

    return context.res.send('')

if __name__ == "__main__":
    menus = get_menus()
    # menus = extract_menus("")
    for menu in menus:
        print(menu)
        # print(json.dumps(menu, default=str))
