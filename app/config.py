"""Local-only configuration; no remote dependencies or dotenv loading."""

import os
import ipaddress
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    converter_url: str = "http://127.0.0.1:8001"
    work_root: Path = (Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "Mobidrill" / "conversion-jobs"
                       if os.name == "nt" else Path(tempfile.gettempdir()) / "mobidrill-conversion-jobs")
    storage_root: Path = Path(__file__).resolve().parents[1] / "resources" / "static"
    max_file_bytes: int = 100 * 1024 * 1024
    multipart_overhead_bytes: int = 1024 * 1024
    conversion_timeout_seconds: int = 60
    max_concurrent_conversions: int = 2
    max_output_bytes: int = 100 * 1024 * 1024
    stale_job_seconds: int = 3600
    max_archive_entries: int = 10000
    max_archive_expanded_bytes: int = 200 * 1024 * 1024

    def __post_init__(self):
        endpoint = urlsplit(self.converter_url)
        try:
            local = ipaddress.ip_address(endpoint.hostname or "").is_loopback
            port = endpoint.port
        except ValueError:
            local, port = False, None
        if (endpoint.scheme != "http" or not local or port is None or endpoint.username is not None
                or endpoint.password is not None or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
            raise ValueError("Converter URL must be a local loopback HTTP endpoint with an explicit port")
        for value in (self.max_file_bytes, self.multipart_overhead_bytes,
                      self.conversion_timeout_seconds, self.max_concurrent_conversions,
                      self.max_output_bytes, self.stale_job_seconds,
                      self.max_archive_entries, self.max_archive_expanded_bytes):
            if value <= 0:
                raise ValueError("Limits must be positive")
        for root in (self.work_root, self.storage_root):
            if root.is_symlink():
                raise ValueError("Storage roots must not be symlinks")
            if str(root).startswith(("\\\\", "//")):
                raise ValueError("Network paths are not allowed for document storage")
            if os.name == "nt":
                import ctypes
                # Reject mapped network drives as well as explicit UNC paths.
                if ctypes.windll.kernel32.GetDriveTypeW(root.absolute().anchor) == 4:
                    raise ValueError("Mapped network drives are not allowed for document storage")
        temporary, permanent = self.work_root.resolve(), self.storage_root.resolve()
        if temporary == permanent or temporary.is_relative_to(permanent) or permanent.is_relative_to(temporary):
            raise ValueError("Temporary and pipeline storage must be separate directories")
        if self.conversion_timeout_seconds > 3600:
            raise ValueError("Conversion timeout must not exceed one hour")

    @classmethod
    def from_env(cls):
        defaults = cls()
        return cls(
            converter_url=os.getenv("CONVERTER_URL", defaults.converter_url).rstrip("/"),
            work_root=Path(os.getenv("CONVERSION_WORK_ROOT", str(defaults.work_root))).absolute(),
            storage_root=Path(os.getenv("PIPELINE_STORAGE_ROOT", str(defaults.storage_root))).absolute(),
            max_file_bytes=int(os.getenv("MAX_FILE_BYTES", defaults.max_file_bytes)),
            conversion_timeout_seconds=int(os.getenv("CONVERSION_TIMEOUT_SECONDS", defaults.conversion_timeout_seconds)),
            max_concurrent_conversions=int(os.getenv("MAX_CONCURRENT_CONVERSIONS", defaults.max_concurrent_conversions)),
            max_output_bytes=int(os.getenv("MAX_OUTPUT_BYTES", defaults.max_output_bytes)),
            stale_job_seconds=int(os.getenv("STALE_JOB_SECONDS", defaults.stale_job_seconds)),
        )
