"""Write small valid one-page PDFs for the sandbox demo into data/docs/. Real merchants use real files."""
import sys
from pathlib import Path


def pdf(text: str) -> bytes:
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"]
    stream = f"BT /F1 14 Tf 20 100 Td ({text}) Tj ET"
    objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()


if __name__ == "__main__":
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "data/docs"); d.mkdir(parents=True, exist_ok=True)
    (d / "product-listing.pdf").write_bytes(pdf("Sandbox product listing (demo)"))
    (d / "delivery-confirmation.pdf").write_bytes(pdf("Sandbox delivery confirmation (demo)"))
    print("wrote", sorted(p.name for p in d.iterdir()))
