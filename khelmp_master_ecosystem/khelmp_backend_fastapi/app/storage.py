from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.config import settings


class StorageBackend(ABC):
    @abstractmethod
    def save(self, source: BinaryIO, filename: str, max_bytes: int) -> tuple[str, int]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        raise NotImplementedError


class LocalStorage(StorageBackend):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, source: BinaryIO, filename: str, max_bytes: int) -> tuple[str, int]:
        suffix = Path(filename).suffix.lower()
        key = f"{uuid4().hex}{suffix}"
        destination = (self.root / key).resolve()
        if destination.parent != self.root:
            raise ValueError("Invalid upload name.")
        size = 0
        try:
            with destination.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("File exceeds the upload size limit.")
                    output.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return key, size

    def delete(self, key: str) -> None:
        path = (self.root / key).resolve()
        if path.parent == self.root:
            path.unlink(missing_ok=True)

    def open(self, key: str) -> BinaryIO:
        path = (self.root / key).resolve()
        if path.parent != self.root:
            raise FileNotFoundError
        return path.open("rb")


storage: StorageBackend = LocalStorage(settings.upload_dir)