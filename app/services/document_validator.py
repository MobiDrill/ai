import logging
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import olefile
from pypdf import PdfReader

from app.config import Settings
from app.exceptions import ConversionError

# Parser warning messages may contain document fragments. Keep them local to a
# sink instead of propagating to application/host log handlers.
parser_logger = logging.getLogger("pypdf")
parser_logger.addHandler(logging.NullHandler())
parser_logger.propagate = False
logger = logging.getLogger(__name__)


class DocumentValidator:
    EXTENSIONS = {".ppt", ".pptx", ".docx", ".pdf", ".hwp"}

    def __init__(self, settings: Settings):
        self.settings = settings

    def validate_input(self, path: Path):
        suffix = path.suffix.lower()
        if suffix not in self.EXTENSIONS:
            raise ConversionError("UNSUPPORTED_FORMAT", 415, "Unsupported document format.")
        if path.stat().st_size == 0:
            raise ConversionError("EMPTY_FILE", 400, "The uploaded file is empty.")
        if suffix == ".pdf":
            return self.validate_pdf(path, output=False)
        logger.debug("input structure inspection format=%s bytes=%d", suffix, path.stat().st_size)
        try:
            if olefile.isOleFile(str(path)):
                with olefile.OleFileIO(str(path)) as document:
                    if document.exists("EncryptedPackage") or document.exists("EncryptionInfo"):
                        raise ConversionError("ENCRYPTED_DOCUMENT", 422, "Password-protected documents are unsupported.")
                    if suffix == ".hwp":
                        # HWP 5.x is not the Hangul WP 97 filter; fail explicitly.
                        if document.exists("FileHeader"):
                            raise ConversionError("UNSUPPORTED_HWP_VERSION", 415, "This HWP version has not been qualified for offline conversion.")
                        raise ValueError("Invalid HWP container")
                    if suffix != ".ppt" or not document.exists("PowerPoint Document"):
                        raise ValueError("Unexpected OLE document")
                return
            if suffix in {".docx", ".pptx"}:
                with ZipFile(path) as archive:
                    entries = archive.infolist()
                    logger.debug("office archive inspected entries=%d expanded_bytes=%d", len(entries), sum(e.file_size for e in entries))
                    if len(entries) > self.settings.max_archive_entries or sum(e.file_size for e in entries) > self.settings.max_archive_expanded_bytes:
                        raise ConversionError("INVALID_DOCUMENT", 400, "Document archive exceeds safety limits.")
                    if any(e.flag_bits & 1 for e in entries):
                        raise ConversionError("ENCRYPTED_DOCUMENT", 422, "Password-protected documents are unsupported.")
                    names = {e.filename for e in entries}
                    required = "word/document.xml" if suffix == ".docx" else "ppt/presentation.xml"
                    if not {"[Content_Types].xml", "_rels/.rels", required}.issubset(names):
                        raise ValueError("Invalid Office archive")
                return
            if suffix == ".hwp":
                with path.open("rb") as stream:
                    if stream.read(32).startswith(b"HWP Document File V3.00"):
                        return
                raise ConversionError("UNSUPPORTED_HWP_VERSION", 415, "Only legacy Hangul WP 97 input is eligible for conversion.")
            raise ValueError("Invalid document")
        except ConversionError:
            raise
        except (OSError, ValueError, BadZipFile, EOFError):
            raise ConversionError("INVALID_DOCUMENT", 400, "Invalid or damaged document.") from None

    def validate_pdf(self, path: Path, output: bool):
        code, status = ("CONVERSION_FAILED", 500) if output else ("INVALID_DOCUMENT", 400)
        try:
            if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= self.settings.max_output_bytes:
                raise ValueError("Invalid PDF file")
            with path.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise ValueError("Invalid PDF header")
                stream.seek(0)
                reader = PdfReader(stream, strict=True)
                if reader.is_encrypted:
                    if output:
                        raise ValueError("Encrypted output")
                    raise ConversionError("ENCRYPTED_DOCUMENT", 422, "Password-protected PDFs are unsupported.")
                if len(reader.pages) == 0:
                    raise ValueError("No pages")
                logger.debug("pdf structure validated kind=%s pages=%d bytes=%d",
                             "output" if output else "input", len(reader.pages), path.stat().st_size)
        except ConversionError:
            raise
        except Exception:
            # Do not expose parser diagnostics (potentially document content).
            raise ConversionError(code, status, "PDF validation failed.") from None
