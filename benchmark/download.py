"""Download/ingest the public benchmark sources into benchmark/data/raw/.

  python benchmark/download.py --source fatura --count 8
  python benchmark/download.py --source cord   --count 5
  python benchmark/download.py --source kaggle --count 4
  python benchmark/download.py --source docile        # access instructions
  python benchmark/download.py --source all
  python benchmark/download.py --normalize            # raw -> data/pdfs

HF sources (fatura, cord) need no credentials. Kaggle needs
~/.kaggle/kaggle.json or KAGGLE_USERNAME/KAGGLE_KEY. DocILE needs the dataset
token from its access-request form (benchmark/sources.yaml) — the function
prints the exact steps instead of guessing.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
RAW = HERE / "data" / "raw"
PDFS = HERE / "data" / "pdfs"

FATURA_URL = "https://zenodo.org/api/records/10371464/files/FATURA2.zip/content"
_FATURA_PREFERRED = ("NUMBER", "DATE", "SELLER_NAME", "BILL_TO",
                     "TOTAL", "SUB_TOTAL", "TAX")
CORD_DATASET = "naver-clova-ix/cord-v2"
KAGGLE_DATASET = "holtskinner/invoices-document-ai"


# --- helpers -----------------------------------------------------------------

def _get(url: str, headers: dict | None = None, timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_meta(path: Path, **fields) -> None:
    path.write_text(json.dumps(fields, indent=2) + "\n", encoding="utf-8")


def _save(rel: str, data: bytes, meta: dict) -> Path:
    out = RAW / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    _write_meta(out.with_suffix(".meta.json"),
                sha256=_sha256(data), bytes=len(data), **meta)
    return out


def _member_bytes(zf: zipfile.ZipFile, name: str) -> bytes:
    """Read one zip member with a single data range request (small members)."""
    import struct
    import zlib
    info = zf.getinfo(name)
    fp = zf.fp
    fp.seek(info.header_offset)
    header = fp.read(30)
    name_len, extra_len = struct.unpack("<HH", header[26:30])
    fp.seek(info.header_offset + 30 + name_len + extra_len)
    raw = fp.read(info.compress_size)
    if info.compress_type == zipfile.ZIP_DEFLATED:
        return zlib.decompress(raw, -15)
    return raw


class _HttpFile(io.RawIOBase):
    """Seekable HTTP range reader so zipfile can read a remote zip in place."""

    def __init__(self, url: str, timeout: float = 120):
        self.url, self.timeout, self.pos = url, timeout, 0
        self._buf, self._buf_off = b"", 0
        with urllib.request.urlopen(
                urllib.request.Request(url, method="HEAD"), timeout=timeout) as r:
            self.size = int(r.headers["Content-Length"])

    def _fetch(self, off: int, size: int) -> bytes:
        end = min(off + size, self.size) - 1
        return _get(self.url, {"Range": f"bytes={off}-{end}"}, self.timeout)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, off: int, whence: int = 0) -> int:
        if whence == 0:
            self.pos = off
        elif whence == 1:
            self.pos += off
        else:
            self.pos = self.size + off
        return self.pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        if not (self._buf_off <= self.pos and self.pos + n <= self._buf_off + len(self._buf)):
            self._buf = self._fetch(self.pos, max(n, 1 << 18))
            self._buf_off = self.pos
        start = self.pos - self._buf_off
        data = self._buf[start:start + n]
        self.pos += len(data)
        return data


# --- FATURA2 -----------------------------------------------------------------

def fetch_fatura(count: int = 8) -> int:
    """Remote-zip read: pick `count` templates, one instance each."""
    zf = zipfile.ZipFile(_HttpFile(FATURA_URL))
    names = zf.namelist()
    images = sorted(n for n in names if n.startswith("invoices_dataset_final/colored_images/"))
    anns = sorted(n for n in names if n.startswith("invoices_dataset_final/Annotations/Original_Format/"))
    by_template: dict[int, list[str]] = {}
    for n in images:
        base = Path(n).stem  # TemplateNN_InstanceMM
        try:
            template = int(base.split("_", 1)[0].removeprefix("Template"))
        except ValueError:
            continue
        by_template.setdefault(template, []).append(n)
    ann_by_instance = {Path(a).stem: a for a in anns}
    # Annotation completeness varies by template: scan every template, score
    # the best instance of each, keep the most complete.
    sample = sorted(by_template)
    candidates: list[tuple[int, int, str, str]] = []
    for template in sample:
        instances = sorted(
            by_template[template],
            key=lambda n: int(Path(n).stem.split("_")[1].removeprefix("Instance")))
        best = (-1, None, None)
        for candidate in instances[:3]:
            ann = ann_by_instance.get(Path(candidate).stem)
            if ann is None:
                continue
            doc = json.loads(_member_bytes(zf, ann))
            score = sum(1 for k in _FATURA_PREFERRED
                        if isinstance(doc.get(k), dict) and doc[k].get("text"))
            if score > best[0]:
                best = (score, candidate, ann)
            if score >= len(_FATURA_PREFERRED) - 1:
                break
        if best[1] is not None:
            candidates.append((best[0], template, best[1], best[2]))
            print(f"fatura: template {template} best {best[0]}/7 ({best[1]})", flush=True)
    candidates.sort(key=lambda c: (-c[0], c[1]))
    chosen = candidates[:count]
    made = 0
    for i, (score, template, member, ann_member) in enumerate(chosen, start=1):
        stem = f"fatura_{i:02d}"
        _save(f"fatura/{stem}.jpg", _member_bytes(zf, member), meta={
            "source": "fatura2", "original_member": member,
            "annotation_member": ann_member,
            "license": "CC BY 4.0",
            "citation": "Limam et al., FATURA (arXiv 2311.11856)",
            "url": "https://huggingface.co/datasets/mathieu1256/FATURA2-invoices",
        })
        _save(f"fatura/{stem}.annotation.json", _member_bytes(zf, ann_member), meta={
            "source": "fatura2", "original_member": ann_member, "license": "CC BY 4.0"})
        made += 1
        print(f"fatura: {stem} <- {member}")
    return made


# --- CORD v2 ------------------------------------------------------------------

def fetch_cord(count: int = 5) -> int:
    made = 0
    offsets = [3, 23, 47, 61, 88, 12, 35, 70, 51, 19,
               7, 29, 41, 55, 66, 74, 81, 90, 96, 15,
               33, 58, 77, 84, 93][:count]
    for i, offset in enumerate(offsets, start=1):
        api = ("https://datasets-server.huggingface.co/rows?"
               f"dataset={CORD_DATASET.replace('/', '%2F')}&config=default"
               f"&split=test&offset={offset}&length=1")
        payload = json.loads(_get(api, timeout=60))
        row = payload["rows"][0]["row"]
        image_url = row["image"]["src"]
        image_bytes = _get(image_url, timeout=120)
        stem = f"cord_{i:02d}"
        _save(f"cord/{stem}.jpg", image_bytes, meta={
            "source": "cord", "split": "test", "index": offset,
            "license": "CC BY 4.0",
            "citation": "Park et al., CORD (NeurIPS Document Intelligence Workshop 2019)",
            "url": "https://huggingface.co/datasets/naver-clova-ix/cord-v2",
        })
        _save(f"cord/{stem}.annotation.json",
              row["ground_truth"].encode("utf-8"), meta={
                  "source": "cord", "split": "test", "index": offset,
                  "license": "CC BY 4.0"})
        made += 1
        print(f"cord: {stem} <- test[{offset}]")
    return made


# --- Kaggle Document AI invoices ----------------------------------------------

def _kaggle_credentials() -> tuple[str, str] | None:
    user, key = os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY")
    if user and key:
        return user, key
    cfg = Path.home() / ".kaggle" / "kaggle.json"
    if cfg.exists():
        data = json.loads(cfg.read_text())
        return data.get("username"), data.get("key")
    return None


def fetch_kaggle(count: int = 4) -> int:
    """Official Kaggle CLI (new KAGGLE_API_TOKEN or legacy kaggle.json)."""
    import config  # benchmark -> Verdandi (one-way) for the shared .env loader
    config.load_dotenv()
    if not (os.environ.get("KAGGLE_API_TOKEN") or all(_kaggle_credentials() or ())):
        print("kaggle: no credentials — set KAGGLE_API_TOKEN in .env or put "
              "kaggle.json in ~/.kaggle/ (dataset: " + KAGGLE_DATASET + ")", file=sys.stderr)
        return 0
    stage = RAW / "kaggle" / ".download"
    stage.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "kaggle", "datasets", "download",
           "-d", KAGGLE_DATASET, "-p", str(stage), "--unzip", "--quiet"]
    subprocess.run(cmd, check=True)
    tifs = sorted(p for p in stage.rglob("*") if p.suffix.lower() == ".tif")
    jsons = {p.stem: p for p in stage.rglob("*.json") if p.suffix.lower() == ".json"}
    made = 0
    for tif in tifs:
        ann = jsons.get(tif.stem)
        if ann is None:
            continue
        i = made + 1
        stem = f"kaggle_{i:02d}"
        _save(f"kaggle/{stem}.tif", tif.read_bytes(), meta={
            "source": "kaggle_docai",
            "original_member": str(tif.relative_to(stage)),
            "license": "CC BY 4.0",
            "citation": "Kaggle: Invoices for Document AI (holtskinner)",
            "labels": "machine-generated (Google Document AI) — verify before freezing",
        })
        _save(f"kaggle/{stem}.annotation.json", ann.read_bytes(), meta={
            "source": "kaggle_docai",
            "original_member": str(ann.relative_to(stage)),
            "license": "CC BY 4.0",
            "labels": "machine-generated — verify before freezing"})
        made += 1
        print(f"kaggle: {stem} <- {tif.name}")
        if made >= count:
            break
    return made


# --- DocILE -------------------------------------------------------------------

def fetch_docile(count: int = 4) -> int:
    token = os.environ.get("DOCILE_TOKEN", "")
    print("docile: access requires the dataset token. Steps:\n"
          "  1. Submit the access-request form (sources.yaml -> docile.url).\n"
          "  2. git clone https://github.com/rossumai/docile /tmp/docile\n"
          "  3. cd /tmp/docile && ./download_dataset.sh <TOKEN> test benchmark/data/raw/docile\n"
          "  4. re-run the adapter once the files are present.\n"
          + (f"  (DOCILE_TOKEN is set, length {len(token)} — follow steps 2-4.)" if token else ""))
    return 0


# --- normalization to PDF -------------------------------------------------------

def normalize() -> int:
    """Convert every raw image into benchmark/data/pdfs/<stem>.pdf."""
    sys.path.insert(0, str(HERE / "synthetic"))
    import pdfwrite  # noqa: E402  (benchmark-local helper)
    from PIL import Image  # noqa: E402
    made = 0
    PDFS.mkdir(parents=True, exist_ok=True)
    for source_dir in sorted(p for p in RAW.iterdir() if p.is_dir()):
        for img in sorted(source_dir.glob("*")):
            if img.suffix.lower() not in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
                continue
            stem = img.stem
            with Image.open(img) as im:
                rgb = im.convert("RGB")
                buf = io.BytesIO()
                rgb.save(buf, "JPEG", quality=90)
            pdfwrite.write_pdf(PDFS / f"{stem}.pdf", [{
                "jpeg": buf.getvalue(), "jw": rgb.width, "jh": rgb.height}])
            (PDFS / f"{stem}.source.json").write_text(json.dumps({
                "raw": str(img.relative_to(HERE)), "pages": 1,
            }, indent=2) + "\n", encoding="utf-8")
            made += 1
            print(f"normalize: {stem}.pdf <- {img.relative_to(HERE)}")
    return made


FETCHERS = {"fatura": fetch_fatura, "cord": fetch_cord,
            "kaggle": fetch_kaggle, "docile": fetch_docile}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fetch public benchmark sources")
    ap.add_argument("--source", choices=[*FETCHERS, "all"], default="all")
    ap.add_argument("--count", type=int, default=None)
    ap.add_argument("--normalize", action="store_true",
                    help="convert raw images to PDFs under data/pdfs")
    args = ap.parse_args(argv)
    if args.normalize:
        print(f"normalized {normalize()} file(s)")
        return 0
    targets = list(FETCHERS) if args.source == "all" else [args.source]
    total = 0
    for name in targets:
        fetcher = FETCHERS[name]
        count = args.count if args.count is not None else {
            "fatura": 8, "cord": 5, "kaggle": 4, "docile": 4}[name]
        total += fetcher(count)
    print(f"fetched {total} document(s) into {RAW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
