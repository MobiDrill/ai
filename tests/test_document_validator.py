import io
import zipfile
from dataclasses import replace

import pytest
from pypdf import PdfWriter

from app.config import Settings
from app.exceptions import ConversionError
from app.services.document_validator import DocumentValidator


def test_valid_pdf(tmp_path, pdf_bytes):
    path = tmp_path / "input.pdf"
    path.write_bytes(pdf_bytes)
    DocumentValidator(Settings()).validate_input(path)


@pytest.mark.parametrize("suffix,data,code", [
    (".docx", b"", "EMPTY_FILE"), (".ppt", b"%PDF-fake", "INVALID_DOCUMENT"),
    (".pdf", b"not a pdf", "INVALID_DOCUMENT"),
    (".hwp", b"unsupported hwp", "UNSUPPORTED_HWP_VERSION"),
    (".exe", b"something", "UNSUPPORTED_FORMAT"),
])
def test_invalid_inputs(tmp_path, suffix, data, code):
    path = tmp_path / ("input" + suffix)
    path.write_bytes(data)
    with pytest.raises(ConversionError) as exc:
        DocumentValidator(Settings()).validate_input(path)
    assert exc.value.code == code


def test_encrypted_pdf(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=200)
    writer.encrypt("secret")
    path = tmp_path / "input.pdf"
    with path.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(ConversionError, match="ENCRYPTED_DOCUMENT"):
        DocumentValidator(Settings()).validate_input(path)


def test_docx_structure_and_archive_limit(tmp_path, docx_bytes):
    path = tmp_path / "input.docx"
    path.write_bytes(docx_bytes)
    DocumentValidator(Settings()).validate_input(path)
    with pytest.raises(ConversionError, match="INVALID_DOCUMENT"):
        DocumentValidator(replace(Settings(), max_archive_expanded_bytes=5)).validate_input(path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("unrelated", "not docx")
    with pytest.raises(ConversionError, match="INVALID_DOCUMENT"):
        DocumentValidator(Settings()).validate_input(path)


def test_output_pdf_missing_or_empty(tmp_path):
    path = tmp_path / "missing.pdf"
    with pytest.raises(ConversionError, match="CONVERSION_FAILED"):
        DocumentValidator(Settings()).validate_pdf(path, output=True)
    writer = PdfWriter()
    with path.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(ConversionError, match="CONVERSION_FAILED"):
        DocumentValidator(Settings()).validate_pdf(path, output=True)

