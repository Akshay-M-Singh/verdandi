"""Document-orientation classifier (D64) — local, deterministic, 4-class.

PP-LCNet_x1_0_doc_ori (Baidu/Paddle, Apache-2.0): classifies a page image as
0°/90°/180°/270° and replaces the D22/D61 geometry A/B, which could not tell
upright from upside-down (Vision reads all orientations equally well).

- Model: `engines/models/PP-LCNet_x1_0_doc_ori.onnx`, converted with the
  official Paddle2ONNX tool from the official inference model
  (`PP-LCNet_x1_0_doc_ori_infer.tar`, paddle-model-ecology.bj.bcebos.com,
  2026-09-30) — SHA256
  `1db9914a3beb04181fde445b2fef96b850072f89a2fa8aa71ebef4ed03b8074f`.
  No official ONNX download exists; the third-party HF conversion measured
  worse (missed 180° on digital pages), so the official conversion is
  committed (7 MB) instead.
- Preprocessing pinned from the model's `inference.yml`: resize short side
  256 → center crop 224 → ImageNet normalize (scale 1/255, mean 0.485/0.456/
  0.406, std 0.229/0.224/0.225) → CHW.
- Output semantics: the label is the COUNTER-CLOCKWISE angle needed to make
  the page upright (PIL convention), so correction is `img.rotate(label)`.
- Confidence: the model is not softmax-calibrated (top probability ~0.45),
  but its logit margin (top1 − top2) is decisive (correct predictions
  measured 0.6–0.9; uncertain/blank pages < 0.5). The caller gates on the
  margin.
- Missing onnxruntime/Pillow degrades gracefully: `available()` is False and
  callers keep the D61 "never rotate" behavior.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("engines.orientation")

MODEL_PATH = Path(__file__).resolve().parent / "models" / "PP-LCNet_x1_0_doc_ori.onnx"
MODEL_SHA256 = "1db9914a3beb04181fde445b2fef96b850072f89a2fa8aa71ebef4ed03b8074f"

# Rotation is applied only when the classifier is decisive: measured margins
# are >=0.6 on correct corpus predictions; uncertain pages (blanks, heavy
# handwriting) sit below 0.5. Below the threshold the page keeps its frame
# (D61 fallback) and stays `unresolved`.
MARGIN_THRESHOLD = 0.5

LABELS = (0, 90, 180, 270)
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

_session = None
_warned = False


def available() -> bool:
    return (MODEL_PATH.exists()
            and importlib.util.find_spec("onnxruntime") is not None
            and importlib.util.find_spec("PIL") is not None)


def _get_session():
    global _session
    if _session is None:
        import onnxruntime as ort
        _session = ort.InferenceSession(str(MODEL_PATH),
                                        providers=["CPUExecutionProvider"])
    return _session


def _preprocess(img):
    """Pinned PaddleX transform (inference.yml): short 256, crop 224, norm."""
    import numpy as np
    img = img.convert("RGB")
    w, h = img.size
    scale = 256.0 / min(w, h)
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                     resample=1)  # 1 = BILINEAR (cv2 INTER_LINEAR equivalent)
    w, h = img.size
    left, top = (w - 224) // 2, (h - 224) // 2
    img = img.crop((left, top, left + 224, top + 224))
    x = np.asarray(img, dtype=np.float32) / 255.0
    x = (x - np.asarray(_MEAN, dtype=np.float32)) / np.asarray(_STD, dtype=np.float32)
    return np.transpose(x, (2, 0, 1))[None].astype(np.float32)


def classify(img) -> tuple[int, float]:
    """(rotation_degrees, margin) for one page image.

    rotation = CCW degrees needed to make the page upright (0 when upright).
    margin = top1 − top2 logit; the caller decides with MARGIN_THRESHOLD.
    Returns (0, 0.0) when the classifier is unavailable.
    """
    global _warned
    if not available():
        if not _warned:
            logger.info("doc-ori classifier unavailable (install onnxruntime + "
                        "Pillow) — keeping the D61 no-rotation behavior")
            _warned = True
        return 0, 0.0
    import numpy as np
    logits = _get_session().run(
        None, {_get_session().get_inputs()[0].name: _preprocess(img)})[0][0]
    order = np.argsort(logits)[::-1]
    return int(LABELS[order[0]]), float(logits[order[0]] - logits[order[1]])


def rotate_point(x: float, y: float, rotation: int) -> tuple[float, float]:
    """Map a bottom-left normalized point through a CCW image rotation."""
    if rotation == 90:
        return (1.0 - y, x)
    if rotation == 180:
        return (1.0 - x, 1.0 - y)
    if rotation == 270:
        return (y, 1.0 - x)
    return (x, y)


def rotate_bbox(x0: float, y0: float, x1: float, y1: float,
                rotation: int) -> tuple[float, float, float, float]:
    """Map a bottom-left normalized bbox through a CCW image rotation."""
    corners = [rotate_point(x0, y0, rotation), rotate_point(x0, y1, rotation),
               rotate_point(x1, y0, rotation), rotate_point(x1, y1, rotation)]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def correct(img, rotation: int):
    """Rotate the image (PIL, CCW) — identity when rotation == 0."""
    if not rotation:
        return img
    return img.rotate(rotation, expand=True)
