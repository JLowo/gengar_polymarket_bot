"""Save everything the bot prints to a log file, one per run.

The terminal output is the best record of a session (fills, fees, results,
errors), but it's gone once the tmux window closes. start_session_log()
makes stdout and stderr write to the terminal as before AND to
logs/bot_<date>_<time>.log, with the time at the start of each line in the
file.

This is done in Python rather than with `| tee` in start.sh: Ctrl+C reaches
every process in the terminal's foreground group, so tee would stop too, and
the bot's shutdown output (order cancel, final balance) would go to a closed
pipe. If the file can't be written (disk full, file removed), the bot stops
writing to it and carries on; the terminal output is never affected.
"""

import os
import re
import sys
import threading
import time
from typing import Optional

_LINES = re.compile(r"[^\n]*\n|[^\n]+")


class _LogFile:
    """The shared log file: stdout and stderr both write here, a line at a time."""

    def __init__(self, path: str):
        self._file = open(path, "a", encoding="utf-8", buffering=1)  # flushed every line
        self._lock = threading.Lock()   # the price feed and Tor threads print too
        self._at_line_start = True
        self.broken = False

    def write(self, text: str):
        if self.broken or not text:
            return
        try:
            with self._lock:
                out = []
                for line in _LINES.findall(text):
                    if self._at_line_start:
                        out.append(time.strftime("%H:%M:%S "))
                    out.append(line)
                    self._at_line_start = line.endswith("\n")
                self._file.write("".join(out))
        except Exception as e:
            self.broken = True
            try:
                sys.__stderr__.write(f"[session log] Stopped writing the log file: {e}\n")
            except Exception:
                pass


class _Tee:
    """A stdout/stderr stand-in: writes go to the terminal, then the log file.
    Everything else (isatty, fileno, encoding...) is the terminal stream's."""

    def __init__(self, stream, log: _LogFile):
        self._stream = stream
        self._log = log

    def write(self, text):
        n = self._stream.write(text)
        self._log.write(text)
        return n

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def start_session_log(log_dir: str = "logs") -> Optional[str]:
    """Start copying stdout and stderr to a new log file in log_dir. Call it
    once, before anything is printed. Returns the file's path, or None if it
    couldn't be created (the bot runs as before, without a log file)."""
    if isinstance(sys.stdout, _Tee):
        return None
    path = os.path.join(log_dir, f"bot_{time.strftime('%Y-%m-%d_%H-%M-%S')}.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        log = _LogFile(path)
    except OSError as e:
        print(f"[session log] Couldn't create a log file in {log_dir}/ ({e}): "
              f"this run's output is only on screen")
        return None
    sys.stdout = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)
    print(f"📝 Session log: {path}")
    return path
