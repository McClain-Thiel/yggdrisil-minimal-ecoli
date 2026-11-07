"""Checked reads for this one-off iCloud-to-S3 migration."""
import json
from pathlib import Path
import time


def read_checked(path: Path) -> bytes:
    for attempt in range(1, 4):
        try:
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
        except TimeoutError as error:
            print(json.dumps({'event':'icloud_read_timeout','path':str(path),'attempt':attempt}), flush=True)
            if attempt == 3:
                raise TimeoutError('iCloud read failed after three attempts: ' + str(path)) from error
        else:
            # iCloud can return an empty read while stat still reports the full file.
            if len(data) == before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
                return data
            print(json.dumps({'event':'icloud_incomplete_read','path':str(path),'attempt':attempt,
                              'read_bytes':len(data),'before_bytes':before.st_size,'after_bytes':after.st_size}), flush=True)
            if attempt == 3:
                raise RuntimeError('File remained incomplete or changed during three reads: ' + str(path))
        time.sleep(2)
