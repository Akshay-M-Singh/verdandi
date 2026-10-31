"""Cross-platform capture engine — pypdfium2 + RapidOCR (D63).

Reproduces the WHOLE Apple harness contract on any OS (Windows/Linux/macOS
without Xcode): routing probe, embedded text + per-line geometry, renders for
the vision pass, OCR with boxes, image stats — same `pageNNN.txt` /
`.bbox.txt` / `.segments.txt` / `report.json` artifacts, so nothing below
`text_layer.py` changes.

- PDF: pypdfium2 (PDFium, Apache-2.0/BSD) — render + per-char text geometry.
- OCR: RapidOCR (PaddleOCR PP-OCRv5/v6 models on ONNX Runtime, Apache-2.0).
  Models download from ModelScope on first use (`out/cache/rapidocr/`).
- Coordinates: every bbox is normalized BOTTOM-LEFT (Apple Vision's
  convention) so grounding's flip math is unchanged.

Deliberate differences from the Apple engine (documented in D63):
- `customWords` has no RapidOCR equivalent (the text channel only verifies;
  the vision pass is primary) — accepted and logged.
- `pageNNN.image.png` (raw embedded photo) is not written; `page_renders`
  falls back to `render-ocr-input.png`, which PDFium renders per spec.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
from pathlib import Path

import engines

logger = logging.getLogger("engines.crossplatform")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_MODEL_CACHE = "VERDANDI_MODEL_CACHE"


def model_cache() -> Path:
    """Where RapidOCR downloads/loads its ONNX models.

    `VERDANDI_MODEL_CACHE` wins; packaged builds must use a stable
    user-writable dir (`~/.verdandi/cache/rapidocr`) because the bundle's
    `_MEIPASS` is read-only or wiped between launches; dev checkouts keep
    `out/cache/rapidocr` so existing caches and tests are unaffected.
    """
    env = os.environ.get(ENV_MODEL_CACHE)
    if env:
        return Path(env).expanduser()
    if getattr(sys, "frozen", False):
        return Path.home() / ".verdandi" / "cache" / "rapidocr"
    return REPO_ROOT / "out" / "cache" / "rapidocr"

DEFAULT_TIMEOUT_S = 600  # per-file; CPU OCR on a long scan is slower than Vision
RENDER_SCALE = 3.0
MAX_RENDER_EDGE = 4000  # same long-edge cap as the Swift renderer

# The RapidOCR engine loads ~30MB of ONNX models (one-time ~15s); keep one
# instance per process so batch runs pay it once.
_OCR_ENGINES: dict[str, object] = {}


def _require(name: str) -> None:
    if importlib.util.find_spec(name) is None:
        raise engines.EngineError(
            f"cross-platform engine needs '{name}' — install "
            "`pip install -r requirements-crossplatform.txt`")


def _ocr_engine(model_root: Path):
    key = str(model_root)
    if key not in _OCR_ENGINES:
        _require("rapidocr")
        from rapidocr import RapidOCR
        model_root.mkdir(parents=True, exist_ok=True)
        _OCR_ENGINES[key] = RapidOCR(params={
            "Global.use_cls": False,      # never rotate text lines (D61)
            "Global.text_score": 0.0,     # keep low-confidence lines (Vision parity)
            "Global.log_level": "warning",
            "Global.model_root_dir": key,
        })
    return _OCR_ENGINES[key]


def _rotate_bbox(x0: float, y0: float, x1: float, y1: float, rotation: int,
                 unrot_w: float, unrot_h: float) -> tuple[float, float, float, float]:
    """Map an unrotated canvas box to the DISPLAYED page frame (bottom-left).

    PDFium text boxes live in the unrotated page space while the render is
    the displayed (/Rotate applied) orientation — the VLM and the reviewer
    see the displayed frame, so geometry must be mapped into it.
    """
    def pt(x: float, y: float) -> tuple[float, float]:
        if rotation == 90:
            return (y, unrot_w - x)
        if rotation == 180:
            return (unrot_w - x, unrot_h - y)
        if rotation == 270:
            return (unrot_h - y, x)
        return (x, y)

    corners = [pt(x0, y0), pt(x0, y1), pt(x1, y0), pt(x1, y1)]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def assemble_lines(fragments: list[dict]) -> dict:
    """Port of the Swift line assembly: y-center clustering + x-sort + gap
    spacing. Fragments carry bottom-left normalized boxes."""
    if not fragments:
        return {"lines": [], "groups": []}
    heights = sorted(f["y1"] - f["y0"] for f in fragments)
    median = heights[len(heights) // 2]
    tol = max(0.002, median * 0.5)
    order = sorted(range(len(fragments)),
                   key=lambda i: (-(fragments[i]["y0"] + fragments[i]["y1"]) / 2,
                                  fragments[i]["x0"]))
    lines: list[dict] = []
    groups: list[list[int]] = []
    for idx in order:
        f = fragments[idx]
        cy = (f["y0"] + f["y1"]) / 2
        if lines and abs((lines[-1]["y0"] + lines[-1]["y1"]) / 2 - cy) <= tol:
            last = lines[-1]
            gap = f["x0"] - last["x1"]
            sep = "    " if gap > 0.03 else (" " if gap > 0.005 else "")
            lines[-1] = {
                "text": last["text"] + sep + f["text"],
                "conf": min(last["conf"], f["conf"]),
                "x0": last["x0"], "y0": min(last["y0"], f["y0"]),
                "x1": f["x1"], "y1": max(last["y1"], f["y1"]),
            }
            groups[-1].append(idx)
        else:
            lines.append(dict(f))
            groups.append([idx])
    return {"lines": lines, "groups": groups}


def _image_stats(img) -> dict:
    """Blank-detection signals on the grayscale render (D16 contract:
    std_dev / dark_ratio / laplacian_variance; float64 for precision)."""
    import numpy as np
    arr = np.asarray(img, dtype=np.float64)
    std_dev = float(arr.std())
    dark_ratio = float((arr < 100).mean())
    lap_var = 0.0
    try:
        import cv2
        lap = cv2.Laplacian(np.asarray(img, dtype=np.uint8), cv2.CV_64F)
        lap_var = float(lap.var())
    except Exception:  # pragma: no cover - cv2 is a rapidocr dependency
        if arr.size:
            lap = (np.roll(arr, 1, 0) + np.roll(arr, -1, 0)
                   + np.roll(arr, 1, 1) + np.roll(arr, -1, 1) - 4 * arr)
            lap_var = float(lap.var())
    return {"std_dev": std_dev, "dark_ratio": dark_ratio,
            "laplacian_variance": lap_var}


class CrossPlatformEngine:
    """pypdfium2 (PDF) + RapidOCR/ONNX (PaddleOCR models)."""

    name = "crossplatform"

    def available(self) -> bool:
        return (importlib.util.find_spec("pypdfium2") is not None
                and importlib.util.find_spec("rapidocr") is not None)

    def describe(self) -> dict:
        return {"name": self.name, "platform": "python",
                "backends": ["pypdfium2", "rapidocr/onnxruntime"]}

    # -- one page ----------------------------------------------------------

    def _process_page(self, doc, reader, index: int, out_dir: Path, *,
                      ocr_only: bool, dump_embedded: bool, vision_cap: int,
                      ocr) -> dict:
        import pypdfium2 as pdfium  # noqa: F401  (documents the dependency)
        from pypdf.generic import ContentStream

        page = doc[index]
        disp_w, disp_h = page.get_size()
        rotation = page.get_rotation()
        unrot_w, unrot_h = ((disp_h, disp_w) if rotation in (90, 270)
                            else (disp_w, disp_h))

        report: dict = {"page": index + 1}
        started = time.time()

        # --- render (displayed orientation, grayscale, long-edge capped) --
        scale = min(RENDER_SCALE, MAX_RENDER_EDGE / max(disp_w, disp_h, 1.0))
        bitmap = page.render(scale=scale, grayscale=True,
                             fill_color=(255, 255, 255, 255))
        img = bitmap.to_pil()
        width, height = img.size
        img.save(out_dir / f"page{index + 1:03d}.render-ocr-input.png")
        if vision_cap > 0:
            long_edge = max(width, height)
            if long_edge > vision_cap:
                ratio = vision_cap / long_edge
                img.resize((max(int(width * ratio), 1), max(int(height * ratio), 1))
                           ).save(out_dir / f"page{index + 1:03d}.render-vision.png")
            else:
                img.save(out_dir / f"page{index + 1:03d}.render-vision.png")
        report["render"] = "page"

        # --- probe signals ------------------------------------------------
        tp = page.get_textpage()
        full_text = tp.get_text_range()
        chars = len(full_text)
        letters = sum(1 for c in full_text if c.isalpha())
        alpha = (letters / chars) if chars else 0.0
        words = len(full_text.split())

        ppage = reader.pages[index]
        text_ops = 0
        invisible = False
        try:
            cs = ContentStream(ppage.get_contents(), reader)
            for operands, op in cs.operations:
                if op in (b"Tj", b"TJ", b"'", b'"'):
                    text_ops += 1
                elif op == b"Tr" and operands and operands[0] == 3:
                    invisible = True
        except Exception as exc:  # malformed stream: no layer signal
            logger.debug("content-stream probe failed on page %d: %s",
                         index + 1, exc)

        img_dims: list[list[int]] = []
        codec = ""
        xobjs = (ppage.get("/Resources") or {}).get("/XObject")
        if xobjs:
            try:
                for _name, ref in xobjs.items():
                    obj = ref.get_object()
                    if obj.get("/Subtype") != "/Image":
                        continue
                    w = int(obj.get("/Width") or 0)
                    h = int(obj.get("/Height") or 0)
                    if w and h:
                        img_dims.append([w, h])
                    filt = obj.get("/Filter")
                    filt = str(filt).lstrip("/") if filt is not None else ""
                    codec = codec or (filt.split(".")[0] if filt else "")
            except Exception as exc:
                logger.debug("XObject probe failed on page %d: %s", index + 1, exc)

        page_area = max(unrot_w * unrot_h, 1.0)
        image_rects = [o.get_bounds() for o in page.get_objects() if o.type == 3]
        coverage = min(sum(max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])
                           for r in image_rects) / page_area, 1.0)
        has_large = any(w * h >= 1_000_000 for w, h in img_dims) and coverage >= 0.5

        report["signals"] = {
            "chars": chars, "alpha": alpha, "words": words,
            "image_coverage": coverage, "invisible_text": invisible,
            "text_ops": text_ops, "codec": codec,
            "images": len(img_dims), "image_decode_failures": 0,
            "image_dims": img_dims, "has_large_image": has_large,
            "top_level_images": len(img_dims),
        }

        # --- routing (same floors as the Swift classify()) ----------------
        if not ocr_only and chars >= 100 and not invisible \
                and (alpha >= 0.5 or words >= 20):
            source = "embedded"
        elif chars == 0 and text_ops == 0 and coverage == 0:
            source = "empty"
        else:
            source = "ocr"
        report["source"] = source

        lines: list[dict] = []
        segments: list[dict] = []
        if source == "embedded":
            lines = self._embedded_lines(tp, full_text, rotation,
                                         unrot_w, unrot_h, disp_w, disp_h)
            report["embedded_geometry"] = "true" if lines else "none"
        elif source == "ocr":
            # D64: classify BEFORE OCR — Paddle reads sideways text poorly
            # (unlike Apple Vision), so the render must be upright before the
            # text channel is built. The text_layer corrector then skips
            # pages already marked `rotated-*`.
            img, label, margin = self._pre_rotate(img, out_dir, index + 1,
                                                  vision_cap, report)
            result = ocr(img.convert("RGB"))
            boxes = result.boxes if result.boxes is not None else []
            txts = result.txts if result.txts is not None else []
            scores = result.scores if result.scores is not None else []
            fragments = []
            for box, text, score in zip(boxes, txts, scores):
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                fragments.append({
                    "text": str(text), "conf": float(score),
                    "x0": min(xs) / width, "x1": max(xs) / width,
                    "y0": 1.0 - max(ys) / height, "y1": 1.0 - min(ys) / height,
                })
            assembled = assemble_lines(fragments)
            lines = assembled["lines"]
            for lineno, group in enumerate(assembled["groups"], start=1):
                for fi in group:
                    f = fragments[fi]
                    segments.append({"lineno": lineno, **f})
            tall = [f for f in fragments
                    if (f["y1"] - f["y0"]) > (f["x1"] - f["x0"])]
            tall_frac = (len(tall) / len(fragments)) if fragments else 0.0
            report["orientation_tall_fraction"] = tall_frac
            if label:
                pass  # D64 pre-rotation already set orientation/confidence
            else:
                sideways = tall_frac > 0.35  # D61 gate, on RAW fragments
                report["orientation"] = "unresolved" if sideways else "upright"
                report["orientation_confidence"] = ("sideways-kept-original"
                                                    if sideways else "upright")
            report["image_stats"] = _image_stats(img)

        if dump_embedded:
            (out_dir / f"page{index + 1:03d}.embedded.txt").write_text(
                full_text, encoding="utf-8")

        # --- write text-channel artifacts ---------------------------------
        page_txt = out_dir / f"page{index + 1:03d}.txt"
        page_txt.write_text("\n".join(l["text"] for l in lines), encoding="utf-8")
        bbox_rows = []
        for lineno, l in enumerate(lines, start=1):
            geo = "{:.4f},{:.4f},{:.4f},{:.4f}".format(
                l["x0"], l["y0"], l["x1"], l["y1"])
            bbox_rows.append(f"{lineno}|{geo}|{l['conf']:.2f}|{l['text']}")
        (out_dir / f"page{index + 1:03d}.bbox.txt").write_text(
            "\n".join(bbox_rows), encoding="utf-8")
        if segments:
            seg_rows = ["{}|{:.4f},{:.4f},{:.4f},{:.4f}|{:.2f}|{}".format(
                s["lineno"], s["x0"], s["y0"], s["x1"], s["y1"],
                s["conf"], s["text"]) for s in segments]
            (out_dir / f"page{index + 1:03d}.segments.txt").write_text(
                "\n".join(seg_rows), encoding="utf-8")

        report["lines"] = len(lines)
        report["words"] = sum(len(l["text"].split()) for l in lines)
        if source == "ocr":
            report["ocr_ms"] = int((time.time() - started) * 1000)
        return report

    @staticmethod
    def _pre_rotate(img, out_dir: Path, page_no: int, vision_cap: int,
                    report: dict):
        """D64 pre-OCR rotation: classify the render; when decisive, rotate
        the image and rewrite the render dumps so OCR, the VLM and the
        reviewer all see the upright frame. Returns (image, label, margin)."""
        from engines import orientation
        if not orientation.available():
            return img, 0, 0.0
        try:
            label, margin = orientation.classify(img)
        except Exception as exc:
            logger.warning("page %d: doc-ori classifier failed: %s", page_no, exc)
            return img, 0, 0.0
        if label == 0 or margin < orientation.MARGIN_THRESHOLD:
            return img, 0, margin
        rotated = orientation.correct(img, label)
        rotated.save(out_dir / f"page{page_no:03d}.render-ocr-input.png")
        if vision_cap > 0:
            long_edge = max(rotated.size)
            if long_edge > vision_cap:
                ratio = vision_cap / long_edge
                rotated.resize((max(int(rotated.width * ratio), 1),
                                max(int(rotated.height * ratio), 1))
                               ).save(out_dir / f"page{page_no:03d}.render-vision.png")
            else:
                rotated.save(out_dir / f"page{page_no:03d}.render-vision.png")
        report["orientation"] = f"rotated-{label}"
        report["orientation_confidence"] = f"doc-ori({margin:.2f})"
        report["render_note"] = (f"orientation corrected: rotated-{label} "
                                 f"(doc-ori classifier, margin {margin:.2f}, D64)")
        return rotated, label, margin

    @staticmethod
    def _embedded_lines(tp, full_text: str, rotation: int,
                        unrot_w: float, unrot_h: float,
                        disp_w: float, disp_h: float) -> list[dict]:
        """PDFium char boxes -> line records (bottom-left normalized).

        Line breaks are PDFium's `\\r\\n`; each line's box is the union of its
        characters' boxes, mapped from unrotated canvas space into the
        displayed frame.
        """
        lines: list[dict] = []
        idx = 0
        for raw_line in full_text.split("\r\n"):
            text = raw_line.strip()
            n = len(raw_line)
            if text:
                xs0, ys0, xs1, ys1 = [], [], [], []
                for i in range(idx, idx + n):
                    ch = tp.get_text_range(i, 1)
                    if not ch.strip():
                        continue
                    l, b, r, t = tp.get_charbox(i)
                    x0, y0, x1, y1 = _rotate_bbox(l, b, r, t, rotation,
                                                  unrot_w, unrot_h)
                    xs0.append(x0); ys0.append(y0); xs1.append(x1); ys1.append(y1)
                if xs0:
                    lines.append({
                        "text": text, "conf": 1.0,
                        "x0": min(xs0) / disp_w, "x1": max(xs1) / disp_w,
                        "y0": min(ys0) / disp_h, "y1": max(ys1) / disp_h,
                    })
            idx += n + 2  # consumed "\r\n"
        return lines

    # -- one file ----------------------------------------------------------

    def process(
        self,
        pdf_path: Path,
        out_dir: Path,
        *,
        custom_words: list[str] | None = None,
        ocr_only: bool = False,
        dump_embedded: bool = False,
        vision_cap: int = 0,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> dict:
        if not self.available():
            raise engines.EngineError(
                "cross-platform engine dependencies are missing — install "
                "`pip install -r requirements-crossplatform.txt`")
        import json

        import pypdfium2 as pdfium
        from pypdf import PdfReader

        if custom_words:
            logger.info("customWords are not supported by RapidOCR (D63) — "
                        "the text channel only verifies; the vision pass is primary")

        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            doc = pdfium.PdfDocument(str(pdf_path))
        except Exception as exc:
            raise RuntimeError(
                f"cannot open PDF (corrupt or unsupported): {pdf_path.name} "
                f"({exc})") from None
        reader = PdfReader(str(pdf_path))
        ocr = _ocr_engine(model_cache())

        page_reports: list[dict] = []
        any_failure = False
        page_count = len(doc)
        try:
            for index in range(page_count):
                try:
                    page_reports.append(self._process_page(
                        doc, reader, index, out_dir, ocr_only=ocr_only,
                        dump_embedded=dump_embedded, vision_cap=vision_cap,
                        ocr=ocr))
                except Exception as exc:
                    logger.error("%s page %d failed: %s",
                                 pdf_path.name, index + 1, exc)
                    page_reports.append({"page": index + 1, "source": "failed",
                                         "error": str(exc)})
                    any_failure = True
        finally:
            doc.close()

        report = {
            "file": pdf_path.name,
            "mode": "ocr-only" if ocr_only else "auto",
            "engine": self.describe(),
            "pages": page_count,
            "pages_report": page_reports,
            "total_ocr_ms": sum(p.get("ocr_ms", 0) for p in page_reports),
            "status": "partial" if any_failure else "ok",
        }
        (out_dir / "report.json").write_text(json.dumps(report, indent=2),
                                             encoding="utf-8")
        return report
