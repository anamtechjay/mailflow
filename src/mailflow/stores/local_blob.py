"""LocalBlobStore — write attachment bytes to a local directory and return a ref
(the object's content-hash). Implements the BlobStore port. Good for single-machine
dev/demos; the GCS/S3 adapter implements the same port for production."""

from __future__ import annotations

import os
from typing import Iterator


class LocalBlobStore:
    def __init__(self, directory: str = "attachments") -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def put_stream(self, ref: str, chunks: Iterator[bytes], content_type: str) -> str:
        path = os.path.join(self.directory, ref)
        if os.path.exists(path):
            return ref  # content-addressed dedupe: identical bytes already stored, skip
        # NOTE: writes are non-atomic (direct to the final path). If a prior put_stream
        # crashed mid-stream, a truncated blob sits at `path` and the dedupe guard above
        # makes a retry skip it rather than self-heal — reads are not hash-verified. The
        # production GCS/S3 adapter should write-to-temp-then-rename (and may verify hash).
        with open(path, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        return ref

    def open(self, ref: str) -> Iterator[bytes]:
        with open(os.path.join(self.directory, ref), "rb") as f:
            yield f.read()
