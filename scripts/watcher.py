import logging
import time
import subprocess
from pathlib import Path

import yaml
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

# ── Config ───────────────────────────────────────────────────────────────────
_CFG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CFG_PATH, encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

_watcher_cfg   = _cfg.get("watcher", {})
WATCH_FOLDER   = _watcher_cfg.get("watch_folder", "/app/input")
COOLDOWN       = int(_watcher_cfg.get("cooldown_seconds", 60))
TRIGGER_PATS   = list(dict.fromkeys(p.upper() for p in _watcher_cfg.get("trigger_patterns", [])))
PYTHON_EXE     = _watcher_cfg.get("python_exe", "python")
SCRIPT_EXTRACT = _watcher_cfg.get("script_extract", "/app/scripts/extract.py")
SCRIPT_PIPELINE = _watcher_cfg.get("script_pipeline", "/app/scripts/pipeline.py")
KNIME_EXE      = _watcher_cfg.get("knime_exe") or None
KNIME_WORKFLOW = _watcher_cfg.get("knime_workflow") or None
LOGS_DIR       = _cfg["paths"]["logs_dir"]

# ── Logging ───────────────────────────────────────────────────────────────────
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
_lc = _cfg.get("logging", {})
logging.basicConfig(
    level=getattr(logging, _lc.get("level", "INFO"), logging.INFO),
    format=_lc.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    datefmt=_lc.get("date_format", "%Y-%m-%d %H:%M:%S"),
    handlers=[
        logging.FileHandler(Path(LOGS_DIR) / "watcher.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("watcher")

# ── Pipeline runners ──────────────────────────────────────────────────────────

def run_knime() -> bool:
    if not KNIME_EXE or not KNIME_WORKFLOW:
        return False
    logger.info("Trying KNIME batch mode...")
    cmd = [
        KNIME_EXE,
        "-nosplash",
        "--launcher.suppressErrors",
        "-application", "org.knime.product.KNIME_BATCH_APPLICATION",
        "-workflowDir", KNIME_WORKFLOW,
        "-reset",
        "-nosave",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode == 0:
        logger.info("KNIME workflow completed successfully")
        return True
    logger.warning("KNIME batch failed (code %d) — falling back to Python...", result.returncode)
    return False

def run_python() -> bool:
    logger.info("Running Python pipeline...")
    for script in [SCRIPT_EXTRACT, SCRIPT_PIPELINE]:
        logger.info("Running %s...", Path(script).name)
        result = subprocess.run(
            [PYTHON_EXE, script],
            capture_output=True,
            text=True,
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                logger.info("[%s] %s", Path(script).name, line)
        if result.returncode != 0:
            logger.error("[%s] failed: %s", Path(script).name, result.stderr.strip())
            return False
    logger.info("Pipeline completed successfully")
    return True

def run_pipeline() -> None:
    if not run_knime():
        run_python()

# ── File system handler ───────────────────────────────────────────────────────

class PDFHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_run = 0

    def _should_trigger(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        name_upper = path.stem.upper()
        return any(pat in name_upper for pat in TRIGGER_PATS)

    def handle(self, path: Path) -> None:
        if not self._should_trigger(path):
            return

        now = time.time()
        if now - self.last_run < COOLDOWN:
            logger.debug("Cooldown active, skipping: %s", path.name)
            return

        logger.info("Detected: %s", path.name)
        self.last_run = now
        time.sleep(3)  # wait for file copy to finish
        run_pipeline()

    def on_created(self, event):
        if not event.is_directory:
            self.handle(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self.handle(Path(event.src_path))

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Watching: %s", WATCH_FOLDER)
    logger.info("Trigger patterns: %s", TRIGGER_PATS)
    logger.info("Drop any matching PDF to trigger pipeline...")

    handler  = PDFHandler()
    observer = PollingObserver()
    observer.schedule(handler, WATCH_FOLDER, recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        logger.info("Watcher stopped")
    observer.join()
