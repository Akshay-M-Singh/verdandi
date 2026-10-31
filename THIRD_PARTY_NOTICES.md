# Third-party notices

Verdandi is licensed under the Apache License, Version 2.0 (`LICENSE`). It
includes or depends on the third-party components below. Each component
remains under its own license; this file records the attribution required by
those licenses.

## Python runtime

| Component | License | Notes |
|---|---|---|
| [PyYAML](https://pyyaml.org/) | MIT | Configuration loading |
| [FastAPI](https://fastapi.tiangolo.com/) | MIT | Desktop engine HTTP API |
| [Starlette](https://www.starlette.io/) | BSD-3-Clause | FastAPI's ASGI toolkit |
| [Uvicorn](https://www.uvicorn.org/) | BSD-3-Clause | Desktop engine server |
| [pydantic](https://docs.pydantic.dev/) | MIT | API request models |
| [keyring](https://pypi.org/project/keyring/) | MIT | OS credential store |
| [openpyxl](https://openpyxl.readthedocs.io/) | MIT | Excel export |

`httpx2` is a test-only dependency (Starlette's test client) and is not
distributed in the application bundle. `kaggle` is benchmark-only
(`benchmark/requirements-benchmark.txt`) and is not distributed either.

## Capture and inference (optional stack)

| Component | License | Notes |
|---|---|---|
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | Apache-2.0 OR BSD-3-Clause | PDF rendering and text geometry |
| [PDFium](https://pdfium.googlesource.com/pdfium/) | BSD-3-Clause | Bundled by pypdfium2 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | Apache-2.0 | OCR engine |
| [PaddleOCR models](https://github.com/PaddlePaddle/PaddleOCR) | Apache-2.0 | PP-OCR detection/recognition models (downloaded at runtime) |
| [ONNX Runtime](https://onnxruntime.ai/) | MIT | Model inference |
| [NumPy](https://numpy.org/) | BSD-3-Clause | Numerical arrays |
| [pypdf](https://pypdf.readthedocs.io/) | BSD-3-Clause | PDF structure probing |
| [Pillow](https://python-pillow.org/) | HPND | Image handling |

### PP-LCNet document-orientation model

`engines/models/PP-LCNet_x1_0_doc_ori.onnx` is a Paddle2ONNX conversion of
Baidu/PaddlePaddle's `PP-LCNet_x1_0_doc_ori` model, licensed under the
Apache License, Version 2.0. Source project:
<https://github.com/PaddlePaddle/PaddleClas>.

## Desktop application

| Component | License | Notes |
|---|---|---|
| [Tauri](https://tauri.app/) | MIT OR Apache-2.0 | Native application shell |
| [Tauri plugins](https://github.com/tauri-apps/plugins-workspace) (`@tauri-apps/*`) | MIT OR Apache-2.0 | Dialog, opener, process, updater, single-instance |
| [React](https://react.dev/) | MIT | User interface |
| [React DOM](https://react.dev/) | MIT | User interface |
| [Radix UI](https://www.radix-ui.com/) | MIT | Accessible UI primitives |
| [shadcn/ui](https://ui.shadcn.com/) | MIT | UI component patterns |
| [Tailwind CSS](https://tailwindcss.com/) | MIT | Styling |
| [lucide-react](https://lucide.dev/) | ISC | Icons |
| [Sonner](https://sonner.emilkowal.ski/) | MIT | Toasts |
| [class-variance-authority](https://cva.style/) | Apache-2.0 | Component variants |
| [clsx](https://github.com/lukeed/clsx) | MIT | Class names |
| [tailwind-merge](https://github.com/dcastil/tailwind-merge) | MIT | Class merging |
| [tw-animate-css](https://github.com/Wombosvideo/tw-animate-css) | MIT | Animation utilities |
| [Geist](https://vercel.com/font) via `@fontsource-variable/geist` | SIL Open Font License 1.1 | Bundled typeface |
| [Vite](https://vite.dev/) | MIT | Frontend build |
| [TypeScript](https://www.typescriptlang.org/) | Apache-2.0 | Type checking |
| [Playwright](https://playwright.dev/) | Apache-2.0 | Screenshot/visual test tooling (development only) |

## Bundling

| Component | License | Notes |
|---|---|---|
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0-or-later **with the PyInstaller bootloader exception** | Packages the Python engine into the desktop sidecar. The exception permits distribution of bundled applications under other licenses. |

## Platform frameworks

The macOS build uses Apple's PDFKit, Vision and WebKit frameworks through the
operating system. They are not redistributed with Verdandi and are governed by
the Apple SDK license terms.

## Benchmark data

The benchmark analysis set under `benchmark/data/` includes documents from
these datasets, each licensed under **Creative Commons Attribution 4.0
International (CC BY 4.0)**:

| Dataset | Attribution | Source |
|---|---|---|
| CORD | Park et al., "CORD: A Consolidated Receipt Dataset for Post-OCR Parsing", NeurIPS Document Intelligence Workshop, 2019 | <https://huggingface.co/datasets/naver-clova-ix/cord-v2> |
| FATURA2 | Limam, Dhiaf, Kessentini, "FATURA: A Multi-Layout Invoice Image Dataset for Document Analysis and Understanding", arXiv:2311.11856, 2023 | <https://huggingface.co/datasets/mathieu1256/FATURA2-invoices> |
| Invoices for Document AI | Kaggle dataset by `holtskinner`, source TIFFs from Kaggle "Text Extraction for OCR" by `manishthem` | <https://www.kaggle.com/datasets/holtskinner/invoices-document-ai> |

Modifications: source documents were normalized to PDF page images, some
pages were re-rendered, and benchmark truth values were re-derived and
verified for this project. Synthetic documents in the same directory were
generated by this project and are covered by the repository license.
DocILE is referenced in `benchmark/sources.yaml` but its data is **not**
included.

See `benchmark/data/NOTICE.md` for the dataset-level attribution.

## Services

The application can talk to an OpenAI-compatible model gateway that the user
configures (the shipped default is the opencode Zen gateway). Those services
are not part of this repository and are governed by their own terms.
