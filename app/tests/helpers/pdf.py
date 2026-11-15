"""Build small PDF files for tests, with or without a text layer."""

from __future__ import annotations


def make_pdf(pages: list[str | None]) -> bytes:
    """A PDF with one page for each item. ``None`` makes a page with no text."""
    objects: list[bytes] = []
    count = len(pages)
    page_ids = [4 + 2 * i for i in range(count)]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content_id = page_ids[i] + 1
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        if text is None:
            stream = b""
        else:
            lines = []
            for n, line in enumerate(text.split("\n")):
                safe = (
                    line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                )
                lines.append(f"BT /F1 12 Tf 72 {720 - 16 * n} Td ({safe}) Tj ET")
            stream = "\n".join(lines).encode("latin-1")
        objects.append(
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)
