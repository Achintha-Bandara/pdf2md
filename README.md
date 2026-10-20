# PDF to Markdown

Convert PDFs into UTF-8 Markdown while preserving document structure where possible: headings, paragraphs, lists, tables, images, and captions. The project includes both a command-line converter and a desktop GUI.

## Requirements

- Python 3.10 or newer
- [PyMuPDF](https://pymupdf.readthedocs.io/)
- Optional: Tesseract OCR for scanned/image-only PDFs

Install the Python dependency:

```powershell
python -m pip install pymupdf
```

For OCR, install Tesseract and the language packs you need. On Windows, make sure the `tesseract` command is available on `PATH`. Auto OCR uses all installed language packs, so it can process mixed-language PDFs.

## Desktop application

Launch the graphical interface:

```powershell
python pdf2md_gui.py
```

1. Select one or more PDF files from any location.
2. Choose the folder in which to save the Markdown files.
3. Leave **OCR language(s), or auto** as `auto` to use all installed Tesseract languages, or enter explicit codes such as `eng+sin+tam`.
4. Select **Convert to Markdown**.

The interface runs conversion in the background and reports files that could not be converted.

## Command line

Convert one PDF:

```powershell
python pdf2md.py document.pdf -o markdown_output
```

Convert every PDF in a folder:

```powershell
python pdf2md.py input_pdfs -o markdown_output --recursive
```

Force OCR for a scanned document and automatically use installed OCR languages:

```powershell
python pdf2md.py scan.pdf -o markdown_output --ocr force --ocr-lang auto
```

Specify only selected OCR languages:

```powershell
python pdf2md.py bilingual.pdf -o markdown_output --ocr force --ocr-lang eng+deu
```

Run `python pdf2md.py --help` for all options.

## Output

For `report.pdf`, the converter creates:

```text
markdown_output/
├── report.md
└── report_assets/
    ├── p001_fig1.png
    └── ...
```

The Markdown file uses UTF-8 encoding. Unicode text is preserved and normalized, including non-Latin scripts such as Sinhala, Tamil, Arabic, Cyrillic, Greek, and CJK languages.

## Notes and limitations

- OCR quality depends on the PDF scan and the installed Tesseract language data.
- `auto` uses all installed recognition packs (excluding Tesseract's `osd` and `equ` helper packs). This supports mixed languages, but may be slower when many packs are installed.
- Password-protected PDFs cannot be converted.
- Complex magazine-style layouts, handwriting, and highly irregular tables may need manual cleanup after conversion.
