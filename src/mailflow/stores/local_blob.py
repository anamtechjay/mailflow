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
        with open(path, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        return ref

    def open(self, ref: str) -> Iterator[bytes]:
        with open(os.path.join(self.directory, ref), "rb") as f:
            yield f.read()
