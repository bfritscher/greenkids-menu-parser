import requests
from bs4 import BeautifulSoup
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

DATA_FOLDER = 'data'
PDF_FOLDER = os.path.join(DATA_FOLDER, 'pdf')
os.makedirs(DATA_FOLDER, exist_ok=True)
os.makedirs(PDF_FOLDER, exist_ok=True)


def find_pdf_links(url):
    """Find PDF links on a page."""
    response = requests.get(url)
    response.raise_for_status()
    pdf_links = re.findall(r'href=["\']([^"\']+\.pdf)["\']', response.text, re.IGNORECASE)
    return list(set(pdf_links))


def find_attachment_links(url):
    """Find attachment_id links on a page that may lead to PDF pages."""
    response = requests.get(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    attachment_links = []
    for link in soup.find_all("a", href=True):
        href = link.get("href")
        if href and "attachment_id=" in href:
            attachment_links.append(href)
    return list(set(attachment_links))


def find_all_pdf_links(start_url):
    """
    Find all PDF links from the starting page.
    Also follows attachment_id links and searches for PDFs on those pages.
    """
    all_pdf_links = []
    
    # First, search for PDFs directly on the starting page
    pdf_links = find_pdf_links(start_url)
    all_pdf_links.extend(pdf_links)
    
    # Then, find attachment_id links and follow them
    attachment_links = find_attachment_links(start_url)
    for attachment_url in attachment_links:
        try:
            pdf_links = find_pdf_links(attachment_url)
            all_pdf_links.extend(pdf_links)
        except Exception:
            # Skip if we can't fetch the attachment page
            pass
    
    return list(set(all_pdf_links))


def url_to_file_path(url):
    filename = url.split("/")[-1]
    # Remove query parameters from filename
    filename = filename.split("?")[0]
    return os.path.join(PDF_FOLDER, filename)


def download_link(url):
    response = requests.get(url)
    response.raise_for_status()
    path = url_to_file_path(url)
    with open(path, "wb") as f:
        f.write(response.content)
    return path


def extract_menus(text):
    """Extract menus from PDF text content."""
    # Days of the week
    days_of_week = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    day_pattern = "|".join(days_of_week)
    
    # Regex pattern to find the day's menu content
    menu_pattern = rf"({day_pattern})\s*(\d{{1,2}})\s*(\w+)(.*?)(?=(?:{day_pattern})\s*\d{{1,2}}\s*\w+|$)"
    
    menus = []
    matches = re.findall(menu_pattern, text, re.DOTALL | re.IGNORECASE)

    year = datetime.datetime.now().year

    for match in matches:
        day = match[0]
        date = match[1]
        month = match[2]
        menu_content = match[3].strip()
        
        # Create a date object using the extracted date and month
        try:
            date_obj = dateparser.parse(f"{date} {month} {year}", languages=['fr'])
        except ValueError:
            date_obj = None
        
        menus.append({
            "dow": day.capitalize(), 
            "date": date_obj,
            "description": menu_content
        })

    return menus


def get_menus(start_url="https://cver.ch"):
    """Get all menus from cver.ch by finding and parsing PDF files."""
    pdf_links = find_all_pdf_links(start_url)
    all_menus = []
    
    for pdf_url in pdf_links:
        try:
            file_path = download_link(pdf_url)
            reader = PdfReader(file_path)
            for page in reader.pages:
                text = page.extract_text()
                menus = extract_menus(text)
                all_menus.extend(menus)
        except Exception:
            # Skip if we can't download or parse the PDF
            pass
    
    return all_menus


def main(context):
    client = Client()

    databases = Databases(client)

    if not os.environ.get('APPWRITE_FUNCTION_ENDPOINT') or not os.environ.get('APPWRITE_FUNCTION_API_KEY'):
        raise Exception(
            'Environment variables are not set. Function cannot use Appwrite SDK.')
    (client
     .set_endpoint(os.environ.get('APPWRITE_FUNCTION_ENDPOINT', None))
     .set_project(os.environ.get('APPWRITE_FUNCTION_PROJECT_ID', None))
     .set_key(os.environ.get('APPWRITE_FUNCTION_API_KEY', None))
     )

    def save_menu(menu):
        return databases.create_document('greenkids', 'menu', ID.unique(), json.dumps(menu, default=str))

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
    for menu in menus:
        print(menu)
        print(json.dumps(menu, default=str))
