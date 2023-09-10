import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
import re
import os
import datetime
import json
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.id import ID

DATA_FOLDER = 'data'
PDF_FOLDER = os.path.join(DATA_FOLDER, 'pdf')
os.makedirs(DATA_FOLDER, exist_ok=True)
os.makedirs(PDF_FOLDER, exist_ok=True)


def find_links():
    url = "https://www.greenkids.biz/nos-menus"
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    links = [(link.get("href"), link.string) for link in soup.find_all(
        "a") if link.find(string=re.compile("^Menus"))]
    return links


def url_to_file_path(url):
    filename = url.split("/")[-1]
    return os.path.join(PDF_FOLDER, filename)


def download_link(url):
    response = requests.get(url)
    path = url_to_file_path(url)
    with open(path, "wb") as f:
        f.write(response.content)
    return path


def extract_text(file_path):
    reader = PdfReader(file_path)
    page = reader.pages[0]
    text = page.extract_text()
    days = ["LUNDI", "MARDI", "MERCREDI", "JEUDI", "VENDREDI", "Allergies"]
    menus = []
    week_number = int(re.search(r'Semaine N° (\d+)', text).group(1))
    # extract first 4 digit number from string
    year = int(re.search(r'\d{4}', text).group(0))
    week_date = datetime.date.fromisocalendar(year, week_number, 1)
    for i in range(len(days) - 1):
        day = days[i]
        if day in text:
            start_index = text.index(day)
            end_index = text.index(days[i+1], start_index + 1)
            menus.append({
                "dow": day,
                "description": text[start_index + len(day):end_index].strip(),
                "date": week_date + datetime.timedelta(days=i),
            })
    return menus


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

    links = find_links()
    new_found = 0
    for url, title in links:
        path = download_link(url)
        menus = extract_text(path)
        for menu in menus:
            try:
                context.log(save_menu(menu))
                new_found += 1
            except Exception as e:
                pass

    if new_found == 0:
        context.error('No new menus found')

    return context.res.send('')
