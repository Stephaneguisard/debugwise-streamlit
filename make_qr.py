"""Usage: python make_qr.py <url> [out.png]   (needs: pip install "qrcode[pil]")"""
import sys
import qrcode

url = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else "qr.png"
qrcode.make(url, box_size=12, border=3).save(out)
print("saved", out, "for", url)
