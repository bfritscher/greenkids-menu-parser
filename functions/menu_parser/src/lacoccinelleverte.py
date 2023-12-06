import requests
from bs4 import BeautifulSoup
import re
import os
import dateparser
import datetime
import json
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.id import ID
import traceback

def extract_text():
    response = requests.get("https://lacoccinelleverte.net/nos-menus/")
    soup = BeautifulSoup(response.text, "html.parser")
    entry_content = soup.select_one(".entry-content")

    for tag_type in ['amp-fit-text', 'strong']:
        for tag in entry_content.find_all(tag_type):
            tag.unwrap()

    entry_content.contents.pop(0)
    entry_content.smooth()
    text = entry_content.get_text(separator='\n')

    date_text = text.split("Lundi")[0]

    date_pattern = r".*?(\d{1,2}).*?(\d{1,2}).*?(\w+).*?(\d{4})"
    match = re.search(date_pattern, text)

    if match:
        start_day = match.group(1)
        end_day = match.group(2)
        month = match.group(3)
        year = match.group(4)

    start_date = dateparser.parse(f"{start_day} {month} {year}", languages=['fr'])

    # Split the text by the days of the week
    days = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi"]
    menus = [
        {"dow": day, "description": text.split(day)[1].split(next_day)[0].strip()}
        if day != "Vendredi"
        else {"dow": "Vendredi", "description": text.split(day)[1].strip()}
        for day, next_day in zip(days, days[1:] + [""])
    ]

    for i, menu in enumerate(menus):
        menu["date"] = start_date + datetime.timedelta(days=i),

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

    new_found = 0
    menus = extract_text()
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
    print(extract_text())
