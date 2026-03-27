import os
import json
from appwrite.client import Client
from appwrite.services.tables_db import TablesDB
from appwrite.services.functions import Functions

DATABASE_ID = "cver"
QUEUE_TABLE = "system_queue"
WORKER_FUNCTION_ID = "queue_worker"


def main(context):
    endpoint = os.environ.get("APPWRITE_FUNCTION_API_ENDPOINT")
    project = os.environ.get("APPWRITE_FUNCTION_PROJECT_ID")
    key = context.req.headers.get("x-appwrite-key")
    if not all([endpoint, project, key]):
        return context.res.send("Missing Appwrite config", 500)

    client = Client()
    client.set_endpoint(endpoint)
    client.set_project(project)
    client.set_key(key)

    tables_db = TablesDB(client)
    functions = Functions(client)

    # Parse event payload (menu document create)
    body = context.req.body
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return context.res.send("Invalid payload", 400)
    if not isinstance(body, dict):
        return context.res.send("Invalid payload", 400)

    menu_id = body.get("$id", body.get("id"))
    if not menu_id:
        return context.res.send("No menu ID in payload", 400)

    # Add job to queue
    tables_db.create_row(
        database_id=DATABASE_ID,
        table_id=QUEUE_TABLE,
        row_id="unique()",
        data={
            "function_id": "menu_image",
            "payload": json.dumps({"id": menu_id}),
            "status": "pending",
            "retries": 0,
        },
    )
    context.log(f"Queued menu_image for menu {menu_id}")

    # Start worker only if no lock exists (worker is already running otherwise)
    try:
        tables_db.get_row(DATABASE_ID, "system_locks", "queue_worker_lock")
        context.log("Worker already active (lock exists), skipping spawn.")
    except Exception:
        try:
            functions.create_execution(
                function_id=WORKER_FUNCTION_ID, body="{}", xasync=True
            )
            context.log("Worker started.")
        except Exception as e:
            context.error(f"Failed to start worker: {e}")

    return context.res.send("Queued.")
