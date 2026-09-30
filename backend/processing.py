"""File processing for Campus Print.

- Converts uploads (images, Word, PowerPoint, Excel) to PDF
- Parses page ranges
- Builds the final print PDF: paper size, orientation, pages per sheet
"""
import os
import shutil
import subprocess
import tempfile
import threading

import fitz  # PyMuPDF

IMAGE_EXTS = {'.jpg', '.jpeg', '.png'}
OFFICE_EXTS = {'.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx'}
ALLOWED_EXTS = {'.pdf'} | IMAGE_EXTS | OFFICE_EXTS

PAPER_SIZES = {'A4': (595, 842), 'A3': (842, 1191)}  # points (1/72 inch)

# pages per sheet -> (columns, rows) on a PORTRAIT sheet; swapped for landscape
GRIDS = {1: (1, 1), 2: (1, 2), 4: (2, 2), 6: (2, 3), 9: (3, 3)}

SOFFICE = shutil.which('soffice') or shutil.which('libreoffice')
_office_lock = threading.Lock()  # LibreOffice cannot run two conversions on one profile


class ProcessingError(Exception):
    """Error message is safe to show to the user."""


# ---------------------------------------------------------------- to PDF

def prepare_pdf(src, dst, ext):
    """Convert `src` into a PDF at `dst`. Returns the page count."""
    try:
        if ext == '.pdf':
            shutil.copyfile(src, dst)
        elif ext in IMAGE_EXTS:
            with fitz.open(src) as img:
                data = img.convert_to_pdf()
            with open(dst, 'wb') as fh:
                fh.write(data)
        elif ext in OFFICE_EXTS:
            _office_to_pdf(src, dst)
        else:
            raise ProcessingError('This file type is not supported.')

        with fitz.open(dst) as doc:
            if doc.needs_pass:
                raise ProcessingError('Password-protected files are not supported.')
            if doc.page_count < 1:
                raise ProcessingError('This file has no pages.')
            return doc.page_count
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError('Could not read this file. It may be corrupt.') from exc


def _office_to_pdf(src, dst):
    if not SOFFICE:
        raise ProcessingError('Word/PowerPoint/Excel conversion is not available right now.')
    profile = os.path.join(tempfile.gettempdir(), 'campus_lo_profile')
    env = {**os.environ, 'HOME': tempfile.gettempdir()}
    with _office_lock, tempfile.TemporaryDirectory() as out_dir:
        cmd = [
            SOFFICE, '--headless', '--norestore', '--nolockcheck',
            f'-env:UserInstallation=file://{profile}',
            '--convert-to', 'pdf', '--outdir', out_dir, src,
        ]
        try:
            subprocess.run(cmd, check=True, timeout=120, capture_output=True, env=env)
        except subprocess.TimeoutExpired:
            raise ProcessingError('Converting this file took too long. Try a PDF instead.')
        except subprocess.CalledProcessError:
            raise ProcessingError('Could not convert this file. Try saving it as PDF.')
        produced = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + '.pdf')
        if not os.path.exists(produced):
            raise ProcessingError('Could not convert this file. Try saving it as PDF.')
        shutil.move(produced, dst)


# ------------------------------------------------------------ page range

def parse_page_range(text, total):
    """'1-3,5' -> [1, 2, 3, 5]. Empty text means all pages. Raises ValueError."""
    text = (text or '').strip()
    if not text:
        return list(range(1, total + 1))
    pages = set()
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            if '-' in part:
                a, b = (int(x) for x in part.split('-', 1))
            else:
                a = b = int(part)
        except ValueError:
            raise ValueError(f'Invalid page range: "{part}"')
        if a < 1 or b < a or b > total:
            raise ValueError(f'Page range "{part}" must be within 1-{total}')
        pages.update(range(a, b + 1))
    if not pages:
        raise ValueError('No pages selected')
    return sorted(pages)


# ---------------------------------------------------------- final layout

def _best_rotation(page_rect, cell):
    """Rotate 90 degrees only when it makes the page noticeably bigger."""
    s0 = min(cell.width / page_rect.width, cell.height / page_rect.height)
    s90 = min(cell.width / page_rect.height, cell.height / page_rect.width)
    return 90 if s90 > s0 * 1.1 else 0


def build_print_pdf(src_pdf, pages, paper, orientation, pps, fit='fit'):
    """Return PDF bytes laid out exactly as it should be printed.

    pages: 1-based page numbers to include
    pps:   pages per sheet (1, 2, 4, 6, 9)
    fit:   'fit' (scale to page) or 'actual' (100%, only when pps == 1)
    """
    sheet_w, sheet_h = PAPER_SIZES[paper]
    cols, rows = GRIDS[pps]
    if orientation == 'landscape':
        sheet_w, sheet_h = sheet_h, sheet_w
        cols, rows = rows, cols

    margin = 0 if pps == 1 else 18
    gap = 0 if pps == 1 else 8
    cell_w = (sheet_w - 2 * margin - gap * (cols - 1)) / cols
    cell_h = (sheet_h - 2 * margin - gap * (rows - 1)) / rows

    with fitz.open(src_pdf) as src, fitz.open() as out:
        sheet = None
        for i, pno in enumerate(pages):
            slot = i % pps
            if slot == 0:
                sheet = out.new_page(width=sheet_w, height=sheet_h)
            col, row = slot % cols, slot // cols
            x0 = margin + col * (cell_w + gap)
            y0 = margin + row * (cell_h + gap)
            cell = fitz.Rect(x0, y0, x0 + cell_w, y0 + cell_h)

            page_rect = src[pno - 1].rect
            if pps == 1 and fit == 'actual':
                x = (sheet_w - page_rect.width) / 2
                y = (sheet_h - page_rect.height) / 2
                target = fitz.Rect(x, y, x + page_rect.width, y + page_rect.height)
                rotate = 0
            else:
                target = cell
                rotate = _best_rotation(page_rect, cell)

            sheet.show_pdf_page(target, src, pno - 1, rotate=rotate)
            if pps > 1:
                sheet.draw_rect(cell, color=(0.7, 0.7, 0.7), width=0.4)

        return out.tobytes(deflate=True, garbage=3)
