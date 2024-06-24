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

DATA_FOLDER = 'data'
PDF_FOLDER = os.path.join(DATA_FOLDER, 'pdf')
os.makedirs(DATA_FOLDER, exist_ok=True)
os.makedirs(PDF_FOLDER, exist_ok=True)

def find_link():
    url = "https://lacoccinelleverte.net/nos-menus/"
    response = requests.get(url)
    response.raise_for_status()
    pdf_links = re.findall(r'="([^"]+\.pdf)"', response.text, re.IGNORECASE)
    return set(pdf_links).pop()

def url_to_file_path(url):
    filename = url.split("/")[-1]
    return os.path.join(PDF_FOLDER, filename)

def download_link(url):
    response = requests.get(url)
    path = url_to_file_path(url)
    with open(path, "wb") as f:
        f.write(response.content)
    return path


def extract_menus(text):
    # Days of the week and a regex pattern to find each day's menu
    days_of_week = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    day_pattern = "|".join(days_of_week)
    
    # Regex pattern to find the day's menu content
    menu_pattern = rf"({day_pattern}) (\d{{1,2}}) (\w+)(.*?)(?=(?:{day_pattern}) \d{{1,2}} \w+|∆ TRIANGLE|$)"
    
    menus = []
    matches = re.findall(menu_pattern, text, re.DOTALL | re.IGNORECASE)

    year = datetime.datetime.now().year

    for match in matches:
        day = match[0]
        date = match[1]
        month = match[2]
        menu_content = match[3].strip().replace(" p\n", "∆\n")
        
        # Create a date object using the extracted date and month and assuming the current year
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

def get_menus():
    file_path = download_link(find_link())
    reader = PdfReader(file_path)
    page = reader.pages[0]
    text = page.extract_text()
    return extract_menus(text)


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
