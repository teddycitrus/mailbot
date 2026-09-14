"""The setup console's resume upload.

The attachment filename is what a stranger sees in their downloads folder, so
the console keeps the name the file arrived with instead of flattening every
upload to resume.pdf.
"""

from __future__ import annotations

import pytest

from src.webapp import uploaded_filename

CRLF = b"\r\n"


def part(filename: bytes) -> bytes:
    """One multipart section, headers and all, as the browser would send it."""
    return (b'Content-Disposition: form-data; name="resume"; filename="'
            + filename + b'"' + CRLF + b"Content-Type: application/pdf"
            + CRLF + CRLF + b"%PDF-1.4 body" + CRLF)


def test_a_named_resume_keeps_its_name():
    assert uploaded_filename(part(b"john_mannully_resume.pdf")) ==         "john_mannully_resume.pdf"


@pytest.mark.parametrize("sent,expected", [
    (rb"C:\\Users\\j\\John Mannully Resume.pdf",
     "John_Mannully_Resume.pdf"),
    (rb"/home/j/resume (final).pdf", "resume_final_.pdf"),
])
def test_directories_are_stripped_and_spaces_are_tamed(sent, expected):
    assert uploaded_filename(part(sent)) == expected


@pytest.mark.parametrize("sent", [
    rb"../../../etc/passwd.pdf",
    rb"..\\..\\evil.pdf",
])
def test_traversal_cannot_escape_the_assets_folder(sent):
    """Only a bare filename survives, so the write stays where it belongs."""
    name = uploaded_filename(part(sent))
    assert "/" not in name and "\\" not in name and ".." not in name


@pytest.mark.parametrize("sent", [rb"notes.txt", rb".pdf", rb""])
def test_anything_that_is_not_a_real_pdf_name_is_refused(sent):
    """An empty answer means the caller keeps the configured RESUME_PATH."""
    assert uploaded_filename(part(sent)) == ""


def test_a_part_without_a_filename_header_is_refused():
    assert uploaded_filename(b"Content-Disposition: form-data" + CRLF + CRLF
                             + b"%PDF-1.4") == ""
