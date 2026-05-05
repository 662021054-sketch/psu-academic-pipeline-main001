#!/usr/bin/env python3
"""Minimal HTTP trigger server — POST /run starts pipeline.py in a background thread."""
import logging
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] trigger_server: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("trigger_server")

_lock = threading.Lock()


def _run_pipeline():
    if not _lock.acquire(blocking=False):
        logger.info("Pipeline already running — trigger ignored")
        return
    try:
        logger.info("Pipeline triggered via HTTP")
        result = subprocess.run(
            ["python", "scripts/pipeline.py"],
            capture_output=True,
            text=True,
        )
        for line in (result.stdout or "").strip().splitlines():
            logger.info("[pipeline] %s", line)
        if result.returncode != 0:
            logger.error("[pipeline] failed (exit %d): %s", result.returncode, result.stderr.strip())
        else:
            logger.info("Pipeline completed successfully")
    finally:
        _lock.release()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/run":
            self.send_response(404)
            self.end_headers()
            return
        threading.Thread(target=_run_pipeline, daemon=True).start()
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"Pipeline triggered\n")

    def log_message(self, fmt, *args):  # suppress default per-request logs
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8502), _Handler)
    logger.info("Trigger server listening on :8502")
    server.serve_forever()
