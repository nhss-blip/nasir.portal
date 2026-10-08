"""Shrinks the big pictures so the page loads fast. Originals are kept in images/original/.
Run inside the container:  /opt/school/venv/bin/python /opt/school/optimize_images.py
Safe to run again: it always starts from the saved originals."""
import shutil, sys
from pathlib import Path
from PIL import Image, ImageSequence

d = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/school/images")
bak = d / "original"; bak.mkdir(exist_ok=True)

def original(name):                       # the untouched copy (saved the first time)
    p, o = d / name, bak / name
    if not o.exists():
        if not p.exists(): return None
        shutil.copy2(p, o)
    return o

def report(name, before):
    p = d / name
    print(f"{name:16} {before/1024:8.0f} KB -> {p.stat().st_size/1024:6.0f} KB")

if (o := original("logo.png")):           # logo: PNG -> WebP (keeps the transparent edges)
    im = Image.open(o).convert("RGBA")
    im.thumbnail((1400, 900), Image.LANCZOS)          # twice the largest size it is shown at
    im.save(d / "logo.webp", quality=88, method=6)
    report("logo.webp", o.stat().st_size)

if (o := original("background.jpg")):     # background: smaller JPEG, same file name
    im = Image.open(o).convert("RGB")
    im.thumbnail((1920, 1200), Image.LANCZOS)
    im.save(d / "background.jpg", quality=78, optimize=True, progressive=True)
    report("background.jpg", o.stat().st_size)

if (o := original("info.webp")):          # animated info icon: smaller frames, same file name
    frames, durs = [], []
    for f in ImageSequence.Iterator(Image.open(o)):
        durs.append(f.info.get("duration", 20))
        frames.append(f.convert("RGBA").resize((96, 96), Image.LANCZOS))
    frames[0].save(d / "info.webp", save_all=True, append_images=frames[1:], duration=durs,
                   loop=0, quality=75, method=6, minimize_size=True)
    report("info.webp", o.stat().st_size)
