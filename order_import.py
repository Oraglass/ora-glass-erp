"""Read a customer's order form and turn the rows into per-size quote lines.

Excel files use a header row and a column map. PDFs use a text table from
pdfplumber or one regular expression per line. Sizes land in inches, which is
the unit the per-size quote stores.
"""

import csv
import os
import re
import secrets
from datetime import datetime, timezone

from storage import atomic_write_json, data_dir, lock_for, read_json

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_EXTENSIONS = {'pdf': 'pdf', 'xlsx': 'excel', 'xls': 'excel', 'csv': 'excel'}
MM_PER_INCH = 25.4
INTERNAL_UNIT = 'in'

_SIZE_RE = re.compile(
    r"""^\s*
        (?:
            (?P<whole>\d+(?:\.\d+)?)
            (?:\s*-\s*|\s+)(?P<num>\d+)\s*/\s*(?P<den>\d+)
            |
            (?P<decimal>\d+(?:\.\d+)?)
            |
            (?P<numonly>\d+)\s*/\s*(?P<denonly>\d+)
        )
        \s*(?P<unit>mm|millimet(?:er|re)s?|in(?:ch(?:es)?)?|")?
        \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_QTY_RE = re.compile(r'^\s*(\d+)(?:\.0+)?\s*(?:pcs|pc|pieces|qty)?\s*$', re.IGNORECASE)
_COLUMN_LETTER = re.compile(r'^[A-Z]{1,2}$')


class OrderImportError(ValueError):
    pass


def uploads_dir():
    path = os.path.join(data_dir(), 'order_uploads')
    os.makedirs(path, exist_ok=True)
    return path


def _path(name):
    return os.path.join(data_dir(), name)


def _load_list(name):
    data = read_json(_path(name), [])
    return data if isinstance(data, list) else []


def _save_list(name, rows):
    path = _path(name)
    with lock_for(path):
        atomic_write_json(path, rows)


def _next_id(rows):
    return max([int(row.get('id') or 0) for row in rows] or [0]) + 1


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_kind(filename):
    ext = os.path.splitext(filename or '')[1].lower().lstrip('.')
    return ALLOWED_EXTENSIONS.get(ext, ''), ext


def safe_filename(filename):
    base = os.path.basename(str(filename or 'order')).replace('\x00', '')
    base = re.sub(r'[^A-Za-z0-9._ -]', '_', base).strip(' .') or 'order'
    return base[:180]


def check_upload(filename, payload):
    kind, ext = file_kind(filename)
    if not kind:
        raise OrderImportError('Upload a PDF, Excel workbook (.xlsx or .xls), or CSV file.')
    if not payload:
        raise OrderImportError('The file is empty.')
    if len(payload) > MAX_UPLOAD_BYTES:
        raise OrderImportError('The file is larger than 8 MB.')
    if ext == 'pdf' and not payload.startswith(b'%PDF'):
        raise OrderImportError('That file is not a PDF.')
    if ext == 'xlsx' and not payload.startswith(b'PK'):
        raise OrderImportError('That file is not an Excel workbook.')
    if ext == 'xls' and not payload.startswith(b'\xd0\xcf\x11\xe0'):
        raise OrderImportError('That file is not an Excel workbook.')
    return kind, ext


def store_upload(filename, payload):
    _kind, ext = check_upload(filename, payload)
    token = secrets.token_hex(8)
    stored_name = f'{token}.{ext}'
    path = os.path.join(uploads_dir(), stored_name)
    with open(path, 'wb') as handle:
        handle.write(payload)
    return stored_name


def upload_abspath(stored_name):
    """Absolute path for a stored upload, or None when it would leave the upload folder."""
    name = os.path.basename(str(stored_name or ''))
    if not name or name != stored_name:
        return None
    root = os.path.realpath(uploads_dir())
    path = os.path.realpath(os.path.join(root, name))
    if path != root and not path.startswith(root + os.sep):
        return None
    if not os.path.isfile(path):
        return None
    return path


def parse_dimension(value):
    """Return (number, explicit unit or None). Unit is 'mm' or 'in'."""
    if isinstance(value, bool):
        raise OrderImportError('Size is not a number.')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0:
            raise OrderImportError('Size must be greater than zero.')
        return float(value), None
    text = str(value or '').strip().replace('″', '"').replace('“', '"').replace('”', '"')
    text = text.replace('″', '"')
    match = _SIZE_RE.match(text)
    if not match:
        raise OrderImportError('Size could not be read.')
    if match.group('numonly'):
        den = int(match.group('denonly'))
        if den == 0:
            raise OrderImportError('Size could not be read.')
        number = int(match.group('numonly')) / den
    else:
        number = float(match.group('whole') or match.group('decimal') or 0)
        if match.group('num'):
            den = int(match.group('den'))
            if den == 0:
                raise OrderImportError('Size could not be read.')
            number += int(match.group('num')) / den
    if number <= 0:
        raise OrderImportError('Size must be greater than zero.')
    unit_token = (match.group('unit') or '').lower()
    if unit_token.startswith('mm') or unit_token.startswith('millimet'):
        unit = 'mm'
    elif unit_token:
        unit = 'in'
    else:
        unit = None
    return number, unit


def to_inches(number, unit):
    if unit == 'mm':
        return round(float(number) / MM_PER_INCH, 4)
    return round(float(number), 4)


def parse_qty(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return value if value > 0 else None
    match = _QTY_RE.match(str(value or ''))
    if not match:
        return None
    qty = int(match.group(1))
    return qty if qty > 0 else None


def _cell_text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f'{value:.10g}'
    return str(value).strip()


def _clean_label(value):
    return re.sub(r'\s+', ' ', _cell_text(value)).strip()


def read_tabular(path, ext):
    """Sheet name to rows. None is the first sheet."""
    if ext == 'csv':
        rows = _read_csv(path)
        return {None: rows, '': rows}
    if ext == 'xlsx':
        return _read_xlsx(path)
    if ext == 'xls':
        return _read_xls(path)
    raise OrderImportError('This file is not a spreadsheet.')


def _read_csv(path):
    last_error = None
    for encoding in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            with open(path, newline='', encoding=encoding) as handle:
                return [list(row) for row in csv.reader(handle)]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise OrderImportError('The CSV file could not be read.') from last_error


def _read_xlsx(path):
    import openpyxl

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    sheets = {}
    try:
        for sheet in workbook.worksheets:
            sheets[sheet.title] = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    if not sheets:
        raise OrderImportError('The workbook has no sheets.')
    sheets[None] = next(iter(sheets.values()))
    return sheets


def _read_xls(path):
    import xlrd

    book = xlrd.open_workbook(path)
    sheets = {}
    for sheet in book.sheets():
        rows = []
        for row_index in range(sheet.nrows):
            rows.append([sheet.cell_value(row_index, col) for col in range(sheet.ncols)])
        sheets[sheet.name] = rows
    if not sheets:
        raise OrderImportError('The workbook has no sheets.')
    sheets[None] = next(iter(sheets.values()))
    return sheets


def read_pdf(path):
    import pdfplumber

    tables = []
    lines = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            lines.extend(text.splitlines())
            for table in page.extract_tables() or []:
                if table:
                    tables.append(table)
    return {'tables': tables, 'lines': lines}


def _column_index(headers, label):
    if not label:
        return None
    wanted = _clean_label(label).casefold()
    for index, header in enumerate(headers):
        if _clean_label(header).casefold() == wanted:
            return index
    token = str(label).strip().upper()
    if _COLUMN_LETTER.match(token):
        index = 0
        for char in token:
            index = index * 26 + (ord(char) - 64)
        index -= 1
        if 0 <= index < max(len(headers), index + 1):
            return index
    return None


def _header_and_body(rows, header_row):
    if header_row < 1 or header_row > len(rows):
        raise OrderImportError('The header row is outside the file.')
    headers = [_clean_label(cell) for cell in rows[header_row - 1]]
    body = rows[header_row:]
    return headers, body


def _mapped_indexes(headers, columns):
    indexes = {}
    missing = []
    for key in ('width', 'height', 'qty', 'glass'):
        label = (columns or {}).get(key) or ''
        index = _column_index(headers, label)
        if index is None:
            missing.append(label or key)
        indexes[key] = index
    for key in ('spacer', 'mark'):
        label = (columns or {}).get(key) or ''
        indexes[key] = _column_index(headers, label) if label else None
    if missing:
        raise OrderImportError('Could not find columns: ' + ', '.join(missing) + '.')
    return indexes


def _cell(row, index):
    if index is None or index >= len(row):
        return ''
    return row[index]


def _blank_row(values):
    return not any(_clean_label(value) for value in values)


def parse_table_rows(rows, template):
    header_row = int(template.get('header_row') or 1)
    headers, body = _header_and_body(rows, header_row)
    indexes = _mapped_indexes(headers, template.get('columns') or {})
    raws = []
    for offset, row in enumerate(body, start=header_row + 1):
        picked = [_cell(row, indexes[key]) for key in ('width', 'height', 'qty', 'glass', 'spacer', 'mark')]
        if _blank_row(picked):
            continue
        raws.append({
            'source_row': offset,
            'width_raw': _clean_label(picked[0]),
            'height_raw': _clean_label(picked[1]),
            'qty_raw': _clean_label(picked[2]),
            'glass_name': _clean_label(picked[3]),
            'spacer_name': _clean_label(picked[4]),
            'mark': _clean_label(picked[5]),
        })
    return _lines_from_raw(raws, template.get('unit') or 'auto')


def parse_regex_lines(text_lines, template):
    pattern = template.get('line_pattern') or ''
    if not pattern or len(pattern) > 300 or pattern.count('(') > 20:
        raise OrderImportError('The PDF line pattern is missing or too long.')
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise OrderImportError('The PDF line pattern is not a valid pattern.') from exc
    raws = []
    for offset, raw_line in enumerate(text_lines, start=1):
        text = raw_line[:500]
        match = compiled.search(text)
        if not match:
            continue
        groups = match.groupdict()
        raws.append({
            'source_row': offset,
            'width_raw': _clean_label(groups.get('width')),
            'height_raw': _clean_label(groups.get('height')),
            'qty_raw': _clean_label(groups.get('qty')),
            'glass_name': _clean_label(groups.get('glass')),
            'spacer_name': _clean_label(groups.get('spacer')),
            'mark': _clean_label(groups.get('mark')),
        })
    return _lines_from_raw(raws, template.get('unit') or 'auto')


def _explicit_unit(value):
    if not _clean_label(value):
        return None
    try:
        _number, unit = parse_dimension(value)
    except OrderImportError:
        return None
    return unit


def _fallback_unit(template_unit, samples):
    if template_unit in {'mm', 'in'}:
        return template_unit
    explicit = [unit for unit in (_explicit_unit(sample) for sample in samples) if unit]
    if explicit:
        return 'mm' if explicit.count('mm') >= explicit.count('in') else 'in'
    numbers = []
    for sample in samples:
        try:
            number, unit = parse_dimension(sample)
        except OrderImportError:
            continue
        if unit is None:
            numbers.append(number)
    if not numbers:
        return 'in'
    numbers.sort()
    median = numbers[len(numbers) // 2]
    return 'mm' if median >= 100 else 'in'


def _lines_from_raw(raws, template_unit):
    truncated = len(raws) > 200
    if truncated:
        raws = raws[:200]
    samples = []
    for raw in raws:
        samples.extend([raw['width_raw'], raw['height_raw']])
    fallback = _fallback_unit(template_unit, samples)
    lines = [_interpret_line(raw, fallback) for raw in raws]
    if truncated:
        lines.append({
            'source_row': 0,
            'mark': '',
            'width_raw': '',
            'height_raw': '',
            'width': None,
            'height': None,
            'unit': INTERNAL_UNIT,
            'qty': 0,
            'glass_name': '',
            'spacer_name': '',
            'errors': ['Only the first 200 lines can be imported.'],
        })
    return _finish_lines(lines, fallback)


def _interpret_line(raw, fallback):
    errors = []
    width = _convert_size(raw['width_raw'], fallback, errors, 'Width')
    height = _convert_size(raw['height_raw'], fallback, errors, 'Height')
    qty = parse_qty(raw['qty_raw'])
    if qty is None:
        errors.append('Quantity could not be read.')
        qty = 0
    if not raw['glass_name']:
        errors.append('Glass name is missing.')
    return {
        'source_row': raw['source_row'],
        'mark': raw['mark'],
        'width_raw': raw['width_raw'],
        'height_raw': raw['height_raw'],
        'width': width,
        'height': height,
        'unit': INTERNAL_UNIT,
        'qty': qty,
        'glass_name': raw['glass_name'],
        'spacer_name': raw['spacer_name'],
        'errors': errors,
    }


def _convert_size(raw, fallback, errors, label):
    if not raw:
        errors.append(f'{label} is missing.')
        return None
    try:
        number, explicit = parse_dimension(raw)
    except OrderImportError:
        errors.append(f'{label} could not be read.')
        return None
    return to_inches(number, explicit or fallback)


def _finish_lines(lines, unit_used):
    usable = [line for line in lines if line.get('width') and line.get('height') and line.get('qty')]
    looks_wrong = not lines or len(usable) * 2 < len([line for line in lines if line.get('source_row')])
    return {
        'lines': lines,
        'unit_used': unit_used,
        'looks_wrong': looks_wrong,
        'error': '' if any(line.get('source_row') for line in lines) else 'No order lines were found.',
    }


def sheet_rows(sheets, template):
    name = (template.get('sheet') or '').strip()
    if name:
        if name not in sheets:
            raise OrderImportError(f'Sheet {name} was not found.')
        return sheets[name]
    return sheets.get(None) or []


def parse_upload(path, ext, template):
    kind = template.get('kind')
    if kind == 'excel':
        if ext == 'pdf':
            raise OrderImportError('This template reads a spreadsheet, not a PDF.')
        sheets = read_tabular(path, ext)
        return parse_table_rows(sheet_rows(sheets, template), template)
    if kind == 'pdf':
        if ext != 'pdf':
            raise OrderImportError('This template reads a PDF.')
        pdf = read_pdf(path)
        mode = template.get('pdf_mode') or 'table'
        if mode == 'regex':
            return parse_regex_lines(pdf['lines'], template)
        last_error = 'No table in the PDF matched this template.'
        for table in pdf['tables']:
            try:
                parsed = parse_table_rows(table, template)
            except OrderImportError as exc:
                last_error = str(exc)
                continue
            if parsed['lines']:
                return parsed
        raise OrderImportError(last_error)
    raise OrderImportError('Unknown template type.')


def _header_hits(template, sheets, pdf):
    columns = template.get('columns') or {}
    needed = [columns.get(key) or '' for key in ('width', 'height', 'qty', 'glass')]
    if not any(needed):
        return 0
    try:
        if template.get('kind') == 'excel':
            headers, _body = _header_and_body(sheet_rows(sheets, template), int(template.get('header_row') or 1))
            return sum(1 for label in needed if label and _column_index(headers, label) is not None)
        if (template.get('pdf_mode') or 'table') == 'regex':
            return 0
        best = 0
        for table in pdf.get('tables') or []:
            try:
                headers, _body = _header_and_body(table, int(template.get('header_row') or 1))
            except OrderImportError:
                continue
            hits = sum(1 for label in needed if label and _column_index(headers, label) is not None)
            best = max(best, hits)
        return best
    except OrderImportError:
        return 0


def choose_template(templates, filename, ext, path):
    """Pick a company template, or ask the user when several match equally."""
    kind, _ext = file_kind(filename)
    usable = [row for row in templates if row.get('kind') == kind and row.get('active', True)]
    if not usable:
        return None, [], 'needs_template'
    if len(usable) == 1:
        return usable[0], [], 'ok'
    sheets = {}
    pdf = {'tables': [], 'lines': []}
    try:
        if kind == 'excel':
            sheets = read_tabular(path, ext)
        else:
            pdf = read_pdf(path)
    except OrderImportError:
        return None, usable, 'choose'
    scored = []
    filename_lower = (filename or '').lower()
    for template in usable:
        score = 0
        token = (template.get('match') or '').strip().lower()
        if token and token in filename_lower:
            score += 10
        hits = _header_hits(template, sheets, pdf)
        score += hits * 3
        if hits >= 4:
            score += 5
        scored.append((score, int(template.get('id') or 0), template))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0][0]
    if best <= 0:
        return None, [item[2] for item in scored], 'choose'
    winners = [item for item in scored if item[0] == best]
    if len(winners) > 1:
        return None, [item[2] for item in winners], 'choose'
    return winners[0][2], [], 'ok'


def load_templates():
    return _load_list('order_templates.json')


def save_templates(rows):
    _save_list('order_templates.json', rows)


def load_mappings():
    data = read_json(_path('order_mappings.json'), {})
    return data if isinstance(data, dict) else {}


def save_mappings(data):
    path = _path('order_mappings.json')
    with lock_for(path):
        atomic_write_json(path, data)


def company_mappings(customer_id):
    stored = load_mappings().get(customer_id) or {}
    glass = stored.get('glass') if isinstance(stored.get('glass'), dict) else {}
    spacer = stored.get('spacer') if isinstance(stored.get('spacer'), dict) else {}
    return {'glass': glass, 'spacer': spacer}


def remember_mappings(customer_id, glass, spacer):
    all_rows = load_mappings()
    current = company_mappings(customer_id)
    for source, product in (glass or {}).items():
        if source and product:
            current['glass'][str(source)] = product
    for source, product in (spacer or {}).items():
        if source and product:
            current['spacer'][str(source)] = product
    all_rows[customer_id] = current
    save_mappings(all_rows)
    return current


def load_imports():
    return _load_list('order_imports.json')


def save_imports(rows):
    _save_list('order_imports.json', rows)


def add_import(record):
    rows = load_imports()
    record = dict(record)
    record['id'] = _next_id(rows)
    record['created_at'] = _now()
    rows.append(record)
    save_imports(rows)
    return record


def replace_import(import_id, record):
    rows = load_imports()
    for index, row in enumerate(rows):
        if row.get('id') == import_id:
            rows[index] = record
            save_imports(rows)
            return record
    return None


def load_requests():
    return _load_list('template_requests.json')


def save_requests(rows):
    _save_list('template_requests.json', rows)


def add_request(record):
    rows = load_requests()
    record = dict(record)
    record['id'] = _next_id(rows)
    record['created_at'] = _now()
    record['status'] = 'open'
    rows.append(record)
    save_requests(rows)
    return record


def open_request_count():
    return sum(1 for row in load_requests() if row.get('status') == 'open')


def public_template(row):
    return {
        'id': row.get('id'),
        'customer_id': row.get('customer_id') or '',
        'name': row.get('name') or '',
        'kind': row.get('kind') or '',
        'unit': row.get('unit') or 'auto',
        'match': row.get('match') or '',
        'sheet': row.get('sheet') or '',
        'header_row': int(row.get('header_row') or 1),
        'columns': row.get('columns') or {},
        'pdf_mode': row.get('pdf_mode') or 'table',
        'line_pattern': row.get('line_pattern') or '',
        'active': bool(row.get('active', True)),
    }


def public_request(row):
    return {
        'id': row.get('id'),
        'import_id': row.get('import_id'),
        'customer_id': row.get('customer_id') or '',
        'company': row.get('company') or '',
        'user': row.get('user') or '',
        'filename': row.get('filename') or '',
        'note': row.get('note') or '',
        'status': row.get('status') or 'open',
        'created_at': row.get('created_at') or '',
    }
