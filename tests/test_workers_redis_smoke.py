"""Real Redis + Celery worker smoke test (SPEC section 8, milestone M2).

This is the one end-to-end check of the worker wiring: a real Redis container is the
broker, a real Celery worker process runs in the background, and the ``fetch_thread``
task runs against a local HTTP stub of the HN API and a file-backed SQLite database.

It is bounded on purpose: worker readiness is polled with a deadline, ``AsyncResult.get``
has a timeout, and the worker process *tree* is stopped in ``finally`` whatever happens.
Marked ``integration`` because it needs Docker.

Teardown order matters and is deliberate: the worker tree dies first, then the in-process
result consumer is stopped and cleared while Redis is still up, then the Redis container
is torn down. Anything left alive across that boundary spends the rest of the run
reconnecting to a Redis that no longer exists.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import redis
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from testcontainers.community.redis import RedisContainer

from skillpulse.models import Base, FetchRun
from skillpulse.repository import count_posts
from skillpulse.workers.app import make_celery_app
from skillpulse.workers.redis_state import RATE_LIMIT_KEY, THREAD_LOCK_PREFIX, RedisRateLimiter
from skillpulse.workers.tasks import FETCH_THREAD_TASK

pytestmark = pytest.mark.integration

THREAD_ID = 1000
COMMENT_IDS = (2001, 2002)
WORKER_READY_TIMEOUT_S = 25.0
TASK_RESULT_TIMEOUT_S = 15.0


def _stop_worker_tree(worker: subprocess.Popen[str]) -> None:
    """Stop a worker and everything it spawned, then reap the process handle.

    On Windows ``sys.executable`` inside a virtual environment is a launcher: it starts
    the real interpreter as a *child* and waits for it. ``terminate()``/``kill()`` would
    only stop that launcher and leave the actual Celery worker running, reconnecting to
    a Redis the container teardown is about to remove. ``taskkill /T`` kills the tree.
    """
    if worker.poll() is None:
        if sys.platform == "win32":
            _taskkill(worker.pid)
        else:
            worker.terminate()
    try:
        worker.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            _taskkill(worker.pid)
        else:
            worker.kill()
        worker.wait(timeout=10)


def _taskkill(pid: int) -> None:
    """Force-kill a process tree by pid (Windows only)."""
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _stub_items() -> dict[str, dict[str, object]]:
    """Canned HN API responses: one hiring thread and its two comments."""

    def comment(item_id: int) -> dict[str, object]:
        return {
            "id": item_id,
            "type": "comment",
            "by": "someone",
            "text": f"<p>post {item_id}</p>",
            "time": 1_700_000_000,
        }

    return {
        f"/item/{THREAD_ID}.json": {
            "id": THREAD_ID,
            "type": "story",
            "by": "whoishiring",
            "title": "Ask HN: Who is hiring? (October 2026)",
            "time": 1_700_000_000,
            "kids": list(COMMENT_IDS),
        },
        f"/item/{COMMENT_IDS[0]}.json": comment(COMMENT_IDS[0]),
        f"/item/{COMMENT_IDS[1]}.json": comment(COMMENT_IDS[1]),
    }


class _StubHandler(BaseHTTPRequestHandler):
    """Serve the canned items; answer anything else with JSON ``null``."""

    items = _stub_items()

    def do_GET(self) -> None:
        payload = self.items.get(self.path)
        body = json.dumps(payload if payload is not None else None).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the stub's access log."""
        return


def test_real_redis_worker_processes_fetch_task(tmp_path: Path) -> None:
    """A real Celery worker, on a real Redis broker, runs ``fetch_thread`` end to end."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    stub_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    db_path = tmp_path / "smoke.db"
    db_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    engine.dispose()

    with RedisContainer("redis:7-alpine") as redis_container:
        redis_url = (
            f"redis://{redis_container.get_container_host_ip()}:"
            f"{redis_container.get_exposed_port(6379)}/0"
        )
        env = {
            **os.environ,
            "SKILLPULSE_REDIS_URL": redis_url,
            "SKILLPULSE_DATABASE_URL": db_url,
            "SKILLPULSE_HN_BASE_URL": f"http://127.0.0.1:{stub_port}",
            "SKILLPULSE_HN_MAX_RETRIES": "0",
            "SKILLPULSE_HN_MAX_REQUESTS_PER_SECOND": "50",
            "SKILLPULSE_LOG_LEVEL": "WARNING",
            # The stub is a local HTTP server, not Hacker News; the live-fetch gate is
            # closed by default, so this path opts in explicitly for the smoke test.
            "SKILLPULSE_HN_LIVE_FETCH_ENABLED": "true",
        }
        # Log to a file rather than a pipe: nothing in the parent can be left waiting on
        # a writer that outlives it, and a failed run still has readable diagnostics.
        worker_log = tmp_path / "worker.log"
        with worker_log.open("w", encoding="utf-8", errors="replace") as log_stream:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "-A",
                    "skillpulse.workers.app",
                    "worker",
                    "--pool=solo",
                    "--loglevel=WARNING",
                ],
                env=env,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                text=True,
            )
        try:
            app = make_celery_app(redis_url)
            deadline = time.monotonic() + WORKER_READY_TIMEOUT_S
            while time.monotonic() < deadline and worker.poll() is None:
                if app.control.ping(timeout=1):
                    break
            assert worker.poll() is None, f"worker exited early:\n{worker_log.read_text()}"
            assert app.control.ping(timeout=1), "Celery worker never became ready"

            redis_client = redis.Redis.from_url(redis_url)

            # 1. A thread another worker already holds must be skipped, not duplicated.
            held = redis_client.lock(f"{THREAD_LOCK_PREFIX}{THREAD_ID}", timeout=60)
            assert held.acquire(blocking=False)
            try:
                skipped = app.send_task(FETCH_THREAD_TASK, args=[THREAD_ID]).get(
                    timeout=TASK_RESULT_TIMEOUT_S
                )
            finally:
                held.release()
            assert skipped["status"] == "locked"

            # 2. With the lock free, the real worker fetches and upserts the thread.
            payload = app.send_task(FETCH_THREAD_TASK, args=[THREAD_ID]).get(
                timeout=TASK_RESULT_TIMEOUT_S
            )

            # 3. The token bucket is in Redis and genuinely throttles a second caller.
            limiter = RedisRateLimiter(redis_client, rate=1.0, capacity=1.0)
            limiter.acquire()
            started = time.monotonic()
            limiter.acquire()
            waited = time.monotonic() - started
            assert waited >= 0.8, f"second token was not throttled (waited {waited:.2f}s)"
            assert redis_client.exists(RATE_LIMIT_KEY) == 1
        finally:
            # Kill the worker tree before anything else: it holds a broker connection
            # and would keep retrying against the Redis container that is about to go.
            _stop_worker_tree(worker)
            # ``AsyncResult.get`` starts the Redis result backend's pubsub consumer;
            # stop it while Redis is still reachable. ``ResultConsumer.stop()`` only
            # closes the pubsub socket and leaves the closed object on the attribute, so
            # a later drain would treat it as live and reconnect to a dying Redis. Clear
            # it to make the consumer genuinely stopped (celery 5.6 result_consumer API).
            result_consumer = getattr(app.backend, "result_consumer", None)
            if result_consumer is not None:
                result_consumer.stop()
                result_consumer._pubsub = None
            redis_client.close()
            server.shutdown()
            server.server_close()

    assert payload["status"] == "ok"
    assert payload["created"] == 2
    assert payload["children_found"] == 2

    engine = create_engine(db_url)
    try:
        with Session(engine) as session:
            assert count_posts(session, THREAD_ID) == 2
            run = session.scalars(select(FetchRun)).one()
            assert run.status == "ok"
            assert run.posts_upserted == 2
    finally:
        engine.dispose()
