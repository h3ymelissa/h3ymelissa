#!/usr/bin/env python3
"""
folder_to_nas_sync.py

Leaves your source folder ALONE until a "trigger" file appears and finishes
writing (your run's "I'm finished" signal). Only THEN does it sync the folder
to the NAS -- and even then it copies only NEW or CHANGED files, never
re-copying what it already sent. The NAS copy keeps the SAME folder name and
the same subfolder structure.

Why the trigger gate: it means files from a run that never completes (crashed,
aborted) are never copied -- nothing moves until the run says it's done.

Safe by default: starts in DRY-RUN mode, which just PRINTS what it *would*
copy without touching anything. Flip DRY_RUN to False once you've watched it
behave on some throwaway test files.

Uses only Python's standard library -- nothing to install.
Stop it any time with Ctrl+C.

NOTE: keep this script OUTSIDE the folder it's watching, so its own log and
state files don't get swept into the copy.
"""

import json
import logging
import shutil
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. SETTINGS  --  edit these, save the file, then run it
# ---------------------------------------------------------------------------

# The folder you want backed up:
SOURCE_FOLDER = r"C:\local_datafolder_test"

# The NAS location to copy INTO. Your folder lands here keeping its own name,
# e.g. SOURCE "run_folder" + DEST "Z:\backups"  ->  "Z:\backups\run_folder".
DEST_FOLDER = r"G:\nanopore\Minion_Raw_Data"

# The "run is finished" signal file INSIDE the source folder. Examples:
#   "final_summary_*.txt"    -> any file starting with final_summary_
#   "sequencing_summary.txt" -> that one exact file
#   "*.done"                 -> any file ending in .done
TRIGGER_PATTERN = "final_summary_*.txt"

# SAFETY SWITCH: True = only print what it would do. False = copy for real.
DRY_RUN = False

# How often to look, in seconds:
POLL_INTERVAL = 5

# The TRIGGER file counts as "done writing" once its size stops changing for
# this many checks in a row:
STABLE_CHECKS = 2

# If a copy fails (e.g. NAS briefly unreachable), retry this many times:
COPY_RETRIES = 3
RETRY_WAIT = 10           # seconds between retries

# Bookkeeping files, kept next to this script:
STATE_FILE = Path(__file__).with_name("copied_state.json")
LOG_FILE = Path(__file__).with_name("watcher.log")


# ---------------------------------------------------------------------------
# 2. LOGGING  --  prints to the screen AND saves to watcher.log
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("sync")


# ---------------------------------------------------------------------------
# 3. REMEMBER WHICH RUNS AND WHICH FILES WE'VE ALREADY HANDLED
# ---------------------------------------------------------------------------
def load_state():
    """Returns (done_runs, done_files) -- two sets of fingerprints."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return set(data.get("runs", [])), set(data.get("files", []))
        except Exception:
            log.warning("Could not read the state file; starting fresh.")
    return set(), set()


def save_state(done_runs, done_files):
    STATE_FILE.write_text(
        json.dumps({"runs": sorted(done_runs), "files": sorted(done_files)}),
        encoding="utf-8",
    )


def signature(rel_path: str, stat) -> str:
    """Fingerprint = path + size + modified-time. A new or changed file
    (or trigger) produces a new fingerprint."""
    return f"{rel_path}|{stat.st_size}|{int(stat.st_mtime)}"


# ---------------------------------------------------------------------------
# 4. COPY ONE FILE (with retries), keeping its place in the folder tree
# ---------------------------------------------------------------------------
def copy_one_file(src_file: Path, rel_path: Path, dest_root: Path) -> bool:
    dest = dest_root / rel_path
    for attempt in range(1, COPY_RETRIES + 1):
        try:
            if DRY_RUN:
                log.info(f"[DRY RUN] would copy: {rel_path}  ->  {dest}")
                return True
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)      # copy2 preserves timestamps
            log.info(f"Copied: {rel_path}  ->  {dest}")
            return True
        except (OSError, shutil.Error) as e:
            log.warning(f"Copy failed (attempt {attempt}/{COPY_RETRIES}) "
                        f"for {rel_path}: {e}")
            if attempt < COPY_RETRIES:
                time.sleep(RETRY_WAIT)
    log.error(f"Giving up on {rel_path} for now; it'll retry on the next run.")
    return False


# ---------------------------------------------------------------------------
# 5. SYNC NEW/CHANGED FILES  --  called only AFTER a run is confirmed done,
#    so we can trust every file is finished and copy without per-file waiting.
# ---------------------------------------------------------------------------
def sync_new_files(source: Path, dest_root: Path, done_files: set) -> int:
    copied = 0
    for path in source.rglob("*"):        # this folder AND all subfolders
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        try:
            stat = path.stat()
        except OSError:
            continue
        sig = signature(str(rel), stat)
        if sig in done_files:
            continue                       # already copied this exact version
        if copy_one_file(path, rel, dest_root):
            done_files.add(sig)
            copied += 1
    return copied


# ---------------------------------------------------------------------------
# 6. ONE PASS: is a run finished? if so, sync it.
# ---------------------------------------------------------------------------
def check_and_sync(source, dest_root, done_runs, done_files, trig_sizes) -> int:
    total = 0
    for trig in source.glob(TRIGGER_PATTERN):
        if not trig.is_file():
            continue
        name = trig.name
        try:
            stat = trig.stat()
        except OSError:
            continue

        # --- Has the trigger file finished writing? ---
        last_size, stable = trig_sizes.get(name, (None, 0))
        stable = stable + 1 if stat.st_size == last_size else 0
        trig_sizes[name] = (stat.st_size, stable)
        if stable < STABLE_CHECKS:
            continue

        # --- Is this a run we haven't synced yet? ---
        run_sig = signature(name, stat)
        if run_sig in done_runs:
            continue

        # --- Run is complete -> sync new/changed files ---
        log.info(f"Run complete (trigger: {name}). Syncing new files...")
        n = sync_new_files(source, dest_root, done_files)
        done_runs.add(run_sig)
        if not DRY_RUN:
            save_state(done_runs, done_files)
        log.info(f"Sync finished: {n} new file(s) copied.")
        total += n
    return total


# ---------------------------------------------------------------------------
# 7. THE MAIN WATCH LOOP  --  runs continuously
# ---------------------------------------------------------------------------
def main():
    source = Path(SOURCE_FOLDER)
    dest_root = Path(DEST_FOLDER) / source.name   # keeps the same folder name

    if not source.is_dir():
        log.error(f"Source folder does not exist: {source}")
        log.error("Fix SOURCE_FOLDER at the top of the script and try again.")
        return

    done_runs, done_files = load_state()
    trig_sizes = {}          # trigger filename -> (last_size, times_stable)

    mode = "DRY RUN (nothing will be copied)" if DRY_RUN else "LIVE (real copies)"
    log.info(f"Watching : {source}")
    log.info(f"Trigger  : {TRIGGER_PATTERN}  (nothing copies until this is done)")
    log.info(f"Mirroring: {dest_root}")
    log.info(f"Mode: {mode}.  Press Ctrl+C to stop.")

    while True:
        try:
            check_and_sync(source, dest_root, done_runs, done_files, trig_sizes)
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("Stopped by user. Goodbye.")
            break
        except Exception as e:
            log.exception(f"Unexpected error (continuing): {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()