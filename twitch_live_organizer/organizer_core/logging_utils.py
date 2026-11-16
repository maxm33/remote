import logging
import os


class ProgressFileHandler(logging.FileHandler):
    PROGRESS_LINE_WIDTH = 256

    def __init__(self, filename, encoding="utf-8"):
        super().__init__(filename, encoding=encoding)
        self.progress_offset = None

    def clear_log(self):
        self.acquire()
        try:
            if self.stream is None:
                raise RuntimeError("Cannot clear a closed log file.")
            self.flush()
            self.stream.seek(0)
            self.stream.truncate()
            self.flush()
            self.progress_offset = None
        finally:
            self.release()

    def write_progress(self, record, complete=False):
        self.acquire()
        try:
            self.flush()
            message = self.format(record).encode(self.encoding or "utf-8")
            if len(message) > self.PROGRESS_LINE_WIDTH:
                raise RuntimeError(
                    "Download progress log entry exceeds its fixed width."
                )

            line = message + b" " * (self.PROGRESS_LINE_WIDTH - len(message))
            newline = os.linesep.encode("ascii")
            with open(self.baseFilename, "r+b") as log_file:
                if self.progress_offset is None:
                    log_file.seek(0, os.SEEK_END)
                    self.progress_offset = log_file.tell()
                log_file.seek(self.progress_offset)
                log_file.write(line + newline)
                log_file.flush()
            if complete:
                self.progress_offset = None
        finally:
            self.release()
