"""Plain data objects wrapping the JSON that slskd returns.

Every model keeps the original payload in ``.raw`` so nothing is lost when
slskd adds fields this package does not know about yet.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath

AUDIO_EXTENSIONS = {
    "mp3", "flac", "wav", "aiff", "aif", "m4a", "aac", "ogg", "opus", "wma", "alac",
}


def _basename(remote_path):
    """Soulseek paths are Windows-style far more often than not."""
    if "\\" in remote_path:
        return PureWindowsPath(remote_path).name
    return PurePosixPath(remote_path).name


@dataclass
class SearchFile:
    """A single file offered by one user in response to a search."""

    username: str
    filename: str
    size: int
    bitrate: int = 0
    length: int = 0
    extension: str = ""
    upload_speed: int = 0
    queue_length: int = 0
    free_upload_slot: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def basename(self):
        return _basename(self.filename)

    @property
    def size_mb(self):
        return self.size / (1024 * 1024)

    @property
    def is_audio(self):
        return self.extension.lower().lstrip(".") in AUDIO_EXTENSIONS

    def __str__(self):
        bits = [f"{self.username}", self.basename, f"{self.size_mb:.1f}MB"]
        if self.bitrate:
            bits.append(f"{self.bitrate}kbps")
        if self.length:
            bits.append(f"{self.length // 60}:{self.length % 60:02d}")
        return " | ".join(bits)

    @classmethod
    def from_json(cls, payload, username, fallback_speed=0, fallback_queue=0,
                  fallback_free_slot=False):
        filename = payload.get("filename", "")
        extension = payload.get("extension") or ""
        if not extension and "." in filename:
            extension = filename.rsplit(".", 1)[-1]
        return cls(
            username=username,
            filename=filename,
            size=payload.get("size", 0),
            bitrate=payload.get("bitRate") or 0,
            length=payload.get("length") or 0,
            extension=extension,
            upload_speed=fallback_speed,
            queue_length=fallback_queue,
            free_upload_slot=fallback_free_slot,
            raw=payload,
        )


@dataclass
class SearchResponse:
    """Everything one user sent back for a search."""

    username: str
    files: list = field(default_factory=list)
    upload_speed: int = 0
    queue_length: int = 0
    free_upload_slot: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, payload):
        username = payload.get("username", "")
        speed = payload.get("uploadSpeed", 0)
        queue = payload.get("queueLength", 0)
        free_slot = payload.get("hasFreeUploadSlot", False)
        files = [
            SearchFile.from_json(f, username, speed, queue, free_slot)
            # slskd splits results into "files" and "lockedFiles"; locked files
            # need permission we do not have, so they are deliberately skipped.
            for f in payload.get("files", [])
        ]
        return cls(
            username=username,
            files=files,
            upload_speed=speed,
            queue_length=queue,
            free_upload_slot=free_slot,
            raw=payload,
        )


@dataclass
class Transfer:
    """A download (or upload) tracked by slskd."""

    id: str
    username: str
    filename: str
    size: int
    state: str
    bytes_transferred: int = 0
    average_speed: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def basename(self):
        return _basename(self.filename)

    @property
    def percent(self):
        if not self.size:
            return 0.0
        return min(100.0, self.bytes_transferred / self.size * 100)

    @property
    def is_complete(self):
        return "Completed" in self.state

    @property
    def is_successful(self):
        return self.state == "Completed, Succeeded"

    @property
    def is_finished(self):
        """Terminal state: nothing more will happen without user action."""
        return self.is_complete

    def __str__(self):
        return f"{self.basename} [{self.state}] {self.percent:.0f}%"

    @classmethod
    def from_json(cls, payload):
        return cls(
            id=payload.get("id", ""),
            username=payload.get("username", ""),
            filename=payload.get("filename", ""),
            size=payload.get("size", 0),
            state=payload.get("state", ""),
            bytes_transferred=payload.get("bytesTransferred", 0),
            average_speed=payload.get("averageSpeed", 0.0),
            raw=payload,
        )
