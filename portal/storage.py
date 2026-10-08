"""Validated upload handling, cover generation and streamed backup archives."""
import tempfile
import warnings
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image
from flask import current_app

MIME = {'pdf': 'application/pdf', 'jpg': 'image/jpeg', 'png': 'image/png',
        'gif': 'image/gif', 'webp': 'image/webp'}


def sniff(header):
    if header[:5] == b'%PDF-':
        return 'pdf'
    if header[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if header[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if header[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'webp'


def validate_pdf(path):
    try:
        with pymupdf.open(path, filetype='pdf') as document:
            if document.needs_pass or not 1 <= len(document) <= 2000:
                raise ValueError
            first = document[0].rect
            if first.width <= 0 or first.height <= 0 or first.width * first.height * 0.49 > 10_000_000:
                raise ValueError
    except Exception:
        raise ValueError('Choose a readable, unencrypted PDF with at least one page and a reasonable page size.') from None


def save_upload(upload, folder, pdf_only=False):
    """Validate a staged file before giving it a public filename."""
    import secrets
    header = upload.stream.read(12)
    upload.stream.seek(0)
    extension = sniff(header)
    if not extension or (pdf_only and (extension != 'pdf' or not upload.filename.lower().endswith('.pdf'))):
        raise ValueError('Choose a PDF, JPG, PNG, GIF or WebP file.' if not pdf_only else 'Choose a PDF file.')
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder, prefix='.upload-', suffix='.part', delete=False) as stream:
        staged = Path(stream.name)
    try:
        upload.save(staged)
        if extension == 'pdf':
            validate_pdf(staged)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(staged) as image:
                    if image.width * image.height > 20_000_000:
                        raise ValueError('The image is too large. Use an image below 20 megapixels.')
                    image.verify()
        destination = folder / (secrets.token_hex(16) + '.' + extension)
        staged.replace(destination)
        return destination, extension
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError('The image could not be read. Please choose a valid image file.') from error
    finally:
        staged.unlink(missing_ok=True)


def make_cover(path):
    output = path.with_suffix('.jpg')
    try:
        with pymupdf.open(path) as document:
            document[0].get_pixmap(matrix=pymupdf.Matrix(0.7, 0.7)).save(str(output))
        return output
    except Exception:
        output.unlink(missing_ok=True)
        current_app.logger.warning('Could not generate a PDF cover.')
        return None


def remove_files(files):
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            current_app.logger.exception('Could not clean up a removed upload.')


def backup_archive():
    """Unique per-request files; archive lives on disk, rather than in a BytesIO."""
    import sqlite3
    temporary = tempfile.TemporaryDirectory(prefix='portal-backup-', dir=current_app.config['DATA_DIR'])
    directory = Path(temporary.name)
    try:
        database = directory / 'school.db'
        with sqlite3.connect(current_app.config['DATABASE']) as source, sqlite3.connect(database) as destination:
            source.backup(destination)
        archive = directory / 'school-backup.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(database, 'school.db')
            uploads = current_app.config['UPLOAD_DIR']
            for path in uploads.rglob('*'):
                if path.is_file() and not path.name.startswith('.upload-'):
                    bundle.write(path, 'uploads/' + path.relative_to(uploads).as_posix())
        return temporary, archive
    except Exception:
        temporary.cleanup()
        raise
