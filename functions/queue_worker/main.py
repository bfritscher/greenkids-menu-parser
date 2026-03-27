import os
import time
from datetime import datetime, timezone
from appwrite.client import Client
from appwrite.services.tables_db import TablesDB
from appwrite.services.functions import Functions
from appwrite.query import Query
from appwrite.exception import AppwriteException

DATABASE_ID = "cver"
QUEUE_TABLE = "system_queue"
LOCK_TABLE = "system_locks"
LOCK_ID = "queue_worker_lock"
LOCK_STALE_SECONDS = 600       # 10 min — treat lock as stale (crashed worker)
POLL_INTERVAL = 5               # seconds between execution polls
POLL_MAX_ATTEMPTS = 65          # 65 * 5s = 325s (> menu_image 300s timeout)
RATE_LIMIT_COOLDOWN = 60        # seconds to wait on rate limit
MAX_RETRIES = 5
MAX_WORKER_RUNTIME = 750        # 12.5 min safety margin (worker timeout 900s)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _init_client(context):
    client = Client()
    client.set_endpoint(os.environ["APPWRITE_FUNCTION_API_ENDPOINT"])
    client.set_project(os.environ["APPWRITE_FUNCTION_PROJECT_ID"])
    client.set_key(context.req.headers["x-appwrite-key"])
    return client


def _acquire_lock(tables_db, context):
    """Try to acquire the worker lock. Returns True if acquired."""
    try:
        tables_db.create_row(
            DATABASE_ID, LOCK_TABLE, LOCK_ID, data={"locked_at": _now_iso()}
        )
        context.log("Lock acquired.")
        return True
    except AppwriteException as e:
        if e.code == 409:
            # Lock exists — check staleness
            try:
                lock = tables_db.get_row(DATABASE_ID, LOCK_TABLE, LOCK_ID)
                locked_at_str = lock.data.get("locked_at", "")
                locked_at = datetime.fromisoformat(
                    locked_at_str.replace("Z", "+00:00")
                )
                age = (datetime.now(timezone.utc) - locked_at).total_seconds()
            except Exception:
                age = LOCK_STALE_SECONDS + 1

            if age >= LOCK_STALE_SECONDS:
                tables_db.update_row(
                    DATABASE_ID, LOCK_TABLE, LOCK_ID,
                    data={"locked_at": _now_iso()},
                )
                context.log(f"Stale lock stolen ({age:.0f}s old).")
                return True
            context.log(f"Lock held by another worker ({age:.0f}s). Exiting.")
            return False
        raise


def _release_lock(tables_db, context):
    try:
        tables_db.delete_row(DATABASE_ID, LOCK_TABLE, LOCK_ID)
        context.log("Lock released.")
    except Exception:
        pass


def _refresh_lock(tables_db):
    try:
        tables_db.update_row(
            DATABASE_ID, LOCK_TABLE, LOCK_ID, data={"locked_at": _now_iso()}
        )
    except Exception:
        pass


def _recover_stuck_jobs(tables_db, context):
    """Reset jobs stuck in 'processing' back to 'pending' (crash recovery)."""
    try:
        result = tables_db.list_rows(
            DATABASE_ID, QUEUE_TABLE,
            queries=[Query.equal("status", "processing")],
        )
        for job in result.rows:
            retries = job.data.get("retries", 0)
            tables_db.update_row(
                DATABASE_ID, QUEUE_TABLE, job.id,
                data={
                    "status": "pending",
                    "retries": retries + 1,
                    "error_log": "Recovered from stuck processing state",
                },
            )
            context.log(f"Recovered stuck job {job.id}")
    except Exception as e:
        context.error(f"Recovery check failed: {e}")


def _poll_execution(job_id, func_id, execution_id, retries, tables_db, functions, context):
    """Poll an execution until completion/failure/timeout.

    Returns True to continue processing queue, False to stop (timeout).
    """
    for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
        time.sleep(POLL_INTERVAL)
        _refresh_lock(tables_db)

        try:
            check = functions.get_execution(
                function_id=func_id, execution_id=execution_id
            )
            status = (
                check.status.value
                if hasattr(check.status, "value")
                else str(check.status)
            )
        except Exception as e:
            context.error(f"Poll error: {e}")
            continue

        if attempt % 6 == 0:  # Log every ~30s
            context.log(f"Poll {attempt}/{POLL_MAX_ATTEMPTS}: {status}")

        if status == "completed":
            tables_db.update_row(
                DATABASE_ID, QUEUE_TABLE, job_id, data={"status": "completed"}
            )
            context.log(f"Job {job_id} completed.")
            return True

        if status == "failed":
            errors = check.errors or ""
            if "429" in errors or "RESOURCE_EXHAUSTED" in errors or "rate" in errors.lower():
                context.log(f"Rate limited on job {job_id}, retry #{retries + 1}")
                tables_db.update_row(
                    DATABASE_ID, QUEUE_TABLE, job_id,
                    data={
                        "status": "pending",
                        "retries": retries + 1,
                        "error_log": errors[:1000],
                    },
                )
                time.sleep(RATE_LIMIT_COOLDOWN)
            else:
                context.error(f"Job {job_id} failed: {errors[:200]}")
                tables_db.update_row(
                    DATABASE_ID, QUEUE_TABLE, job_id,
                    data={"status": "failed", "error_log": errors[:1000]},
                )
            return True

    # Poll timeout — keep execution_id so next worker can check it
    context.error(
        f"Job {job_id} poll timeout ({POLL_MAX_ATTEMPTS * POLL_INTERVAL}s)"
    )
    tables_db.update_row(
        DATABASE_ID, QUEUE_TABLE, job_id,
        data={
            "status": "pending",
            "retries": retries + 1,
            "error_log": f"Poll timeout (execution {execution_id})",
        },
    )
    return False


def _process_one_job(job, functions, tables_db, context):
    """Process a single queue job. Returns True to continue, False to stop."""
    job_id = job.id
    func_id = job.data.get("function_id")
    payload = job.data.get("payload") or "{}"
    retries = job.data.get("retries", 0)

    if not func_id:
        tables_db.update_row(
            DATABASE_ID, QUEUE_TABLE, job_id,
            data={"status": "failed", "error_log": "Missing function_id"},
        )
        return True

    if retries >= MAX_RETRIES:
        tables_db.update_row(
            DATABASE_ID, QUEUE_TABLE, job_id,
            data={
                "status": "failed",
                "error_log": f"Max retries ({MAX_RETRIES}) exceeded",
            },
        )
        context.log(f"Job {job_id} exceeded max retries.")
        return True

    # Check if a previous execution already ran (dedup on retry)
    prev_execution_id = job.data.get("execution_id")
    if prev_execution_id:
        try:
            check = functions.get_execution(
                function_id=func_id, execution_id=prev_execution_id
            )
            status = (
                check.status.value
                if hasattr(check.status, "value")
                else str(check.status)
            )
            if status == "completed":
                tables_db.update_row(
                    DATABASE_ID, QUEUE_TABLE, job_id,
                    data={"status": "completed"},
                )
                context.log(
                    f"Job {job_id} already completed (exec {prev_execution_id})."
                )
                return True
            if status in ("waiting", "processing"):
                context.log(
                    f"Job {job_id} exec {prev_execution_id} still {status}, resuming poll."
                )
                tables_db.update_row(
                    DATABASE_ID, QUEUE_TABLE, job_id,
                    data={"status": "processing"},
                )
                return _poll_execution(
                    job_id, func_id, prev_execution_id, retries,
                    tables_db, functions, context,
                )
            context.log(
                f"Job {job_id} prev exec {prev_execution_id} "
                f"status={status}, starting new."
            )
        except Exception as e:
            context.error(f"Prev execution check failed: {e}")

    # Mark as processing
    tables_db.update_row(
        DATABASE_ID, QUEUE_TABLE, job_id, data={"status": "processing"}
    )

    try:
        execution = functions.create_execution(
            function_id=func_id, body=payload, xasync=True
        )
        execution_id = execution.id
        context.log(f"Started {func_id} execution {execution_id}")
    except Exception as e:
        tables_db.update_row(
            DATABASE_ID, QUEUE_TABLE, job_id,
            data={"status": "failed", "error_log": str(e)[:1000]},
        )
        context.error(f"Failed to start {func_id}: {e}")
        return True

    # Store execution_id for dedup on retry
    try:
        tables_db.update_row(
            DATABASE_ID, QUEUE_TABLE, job_id,
            data={"execution_id": execution_id},
        )
    except Exception:
        pass

    return _poll_execution(
        job_id, func_id, execution_id, retries,
        tables_db, functions, context,
    )


def main(context):
    worker_start = time.monotonic()
    client = _init_client(context)
    tables_db = TablesDB(client)
    functions = Functions(client)

    try:
        if not _acquire_lock(tables_db, context):
            return context.res.send("Another worker active.")
    except Exception as e:
        context.error(f"Lock acquisition failed: {e}")
        return context.res.send("Lock error.")

    has_more = False
    try:
        _recover_stuck_jobs(tables_db, context)

        while True:
            elapsed = time.monotonic() - worker_start
            if elapsed > MAX_WORKER_RUNTIME:
                context.log(f"Time budget exceeded ({elapsed:.0f}s).")
                has_more = True
                break

            jobs = tables_db.list_rows(
                DATABASE_ID, QUEUE_TABLE,
                queries=[
                    Query.equal("status", "pending"),
                    Query.order_asc("$createdAt"),
                    Query.limit(1),
                ],
            )
            if not jobs.rows:
                context.log("Queue empty.")
                break

            if not _process_one_job(jobs.rows[0], functions, tables_db, context):
                has_more = True
                break

            _refresh_lock(tables_db)
    except Exception as e:
        context.error(f"Worker error: {e}")
        has_more = True
    finally:
        _release_lock(tables_db, context)

    if has_more:
        try:
            functions.create_execution(
                function_id=os.environ.get("APPWRITE_FUNCTION_ID", "queue_worker"),
                body="{}",
                xasync=True,
            )
            context.log("Chained next worker.")
        except Exception as e:
            context.error(f"Chain failed: {e}")

    return context.res.send("Worker done.")
