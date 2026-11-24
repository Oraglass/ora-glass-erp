"""Read an ORGADATA GLASS ORDER PDF into a per-size quote.

The file is a text PDF from combit List & Label. The heading on the page is
still "Glass/Panel Order". A scan with no text layer is refused. Position /
Number is a floor code. The production product ID is looked up later from the
IGU build and is never invented here.
"""

import os
import re

from storage import atomic_write_json, data_dir, lock_for, read_json

SOURCE_LABEL = 'ORGADATA GLASS ORDER'
HEADING = 'Glass/Panel Order'
SCANNED_MESSAGE = 'scanned PDF not supported yet'

_PAGE_RE = re.compile(r'^page\s+\d+\s*$', re.IGNORECASE)
_DATE_RE = re.compile(r'^Date:\s*(\d{4}-\d{2}-\d{2})\b', re.IGNORECASE)
_PROJECT_RE = re.compile(
    r'^Project name:\s*(?P<project>.*?)\s+Job number:\s*(?P<po>\S+)\s*$',
    re.IGNORECASE,
)
_CONTACT_RE = re.compile(r'^Person in Charge:\s*(?P<name>.+?)\s*$', re.IGNORECASE)
_THICKNESS_RE = re.compile(
    r'^Thickness:\s*(?P<body>.+?)(?:\s+Spacer:\s*(?P<spacer>.+))?$',
    re.IGNORECASE,
)
_ROW_RE = re.compile(
    r'^(?P<floor>[A-Za-z]{1,6}\d{1,4}\s*/\s*\d+)\s+'
    r'(?P<qty>\d+)\s+'
    r'(?P<width>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s+'
    r'(?P<height>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?:\s|$)',
)
_SUM_RE = re.compile(r'^Sum:\s*(?P<qty>\d+)\b', re.IGNORECASE)
_SUM_QTY_RE = re.compile(r'Sum Quantity:\s*(?P<qty>\d+)\s*Pcs', re.IGNORECASE)
_PANE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*mm', re.IGNORECASE)
_MM_VALUE_RE = re.compile(r'(\d+)\s*mm', re.IGNORECASE)
_CLEAR = {
    'planibel g', 'planibel clear', 'clear', 'clear glass', 'float', 'float glass',
}
_MAKERS = {'agc', 'guardian', 'vitro', 'cardinal', 'pilkington'}
_SKIP_TOKENS = {'tempered', 'argon', 'annealed', 'and', 'with', 'the', 'mm'}


class OrgadataImportError(ValueError):
    pass


def _collapse(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _norm(value):
    text = _collapse(value).casefold().replace('aluminium', 'aluminum')
    text = re.sub(r'\([^)]*\)', ' ', text)
    text = re.sub(r'\b\d+(?:\.\d+)?\s*mm\b', ' ', text)
    text = re.sub(r'[^a-z0-9+./ -]+', ' ', text)
    return _collapse(text)


def _number(token):
    return float(str(token).replace(',', '').replace(' ', ''))


def _floor(value):
    text = _collapse(value)
    return re.sub(r'\s*/\s*', ' / ', text)


def _path(name):
    return os.path.join(data_dir(), name)


def load_imports():
    rows = read_json(_path('orgadata_imports.json'), [])
    return rows if isinstance(rows, list) else []


def save_imports(rows):
    path = _path('orgadata_imports.json')
    with lock_for(path):
        atomic_write_json(path, rows)


def add_import(record):
    rows = load_imports()
    stored = dict(record)
    stored['id'] = max([int(row.get('id') or 0) for row in rows] or [0]) + 1
    rows.append(stored)
    save_imports(rows)
    return stored


def replace_import(import_id, record):
    rows = load_imports()
    for index, row in enumerate(rows):
        if row.get('id') == import_id:
            rows[index] = record
            save_imports(rows)
            return record
    return None


def load_aliases():
    data = read_json(_path('orgadata_aliases.json'), {})
    return data if isinstance(data, dict) else {}


def aliases_for(customer_id):
    stored = load_aliases().get(customer_id or '') or {}
    glass = stored.get('glass') if isinstance(stored.get('glass'), dict) else {}
    spacer = stored.get('spacer') if isinstance(stored.get('spacer'), dict) else {}
    return {'glass': {str(key): value for key, value in glass.items()},
            'spacer': {str(key): value for key, value in spacer.items()}}


def remember_aliases(customer_id, glass, spacer):
    """Remember source text to an ERP product. Blank keys are ignored."""
    if not customer_id:
        return aliases_for('')
    all_rows = load_aliases()
    current = aliases_for(customer_id)
    for source, product in (glass or {}).items():
        key = _norm(source)
        if key and product:
            current['glass'][key] = product
    for source, product in (spacer or {}).items():
        key = _norm(source)
        if key and product:
            current['spacer'][key] = product
    all_rows[customer_id] = current
    path = _path('orgadata_aliases.json')
    with lock_for(path):
        atomic_write_json(path, all_rows)
    return current


def _lines_from_words(words):
    """Rebuild reading order from word boxes. Words on one baseline stay one line."""
    rows = []
    for word in sorted(words or [], key=lambda item: (round(float(item.get('top') or 0), 1), float(item.get('x0') or 0))):
        text = _collapse(word.get('text'))
        if not text:
            continue
        top = float(word.get('top') or 0)
        if rows and abs(top - rows[-1]['top']) <= 2.5:
            rows[-1]['words'].append(text)
        else:
            rows.append({'top': top, 'words': [text]})
    return [' '.join(row['words']) for row in rows]


def read_pdf_text(path):
    """Layout text from a text PDF. A PDF with no text layer is refused."""
    import pdfplumber

    pages = []
    saw_text = False
    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            raise OrgadataImportError('The PDF has no pages.')
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
            if not words and page.chars:
                words = _words_from_chars(page.chars)
            if words or page.chars:
                saw_text = True
            pages.append(_lines_from_words(words))
    if not saw_text:
        raise OrgadataImportError(SCANNED_MESSAGE)
    return '\n'.join(line for page in pages for line in page)


def _words_from_chars(chars):
    groups = []
    for char in sorted(chars, key=lambda item: (float(item.get('top') or 0), float(item.get('x0') or 0))):
        text = str(char.get('text') or '')
        if not text or text.isspace():
            continue
        top = float(char.get('top') or 0)
        x0 = float(char.get('x0') or 0)
        x1 = float(char.get('x1') or x0)
        if groups and abs(top - groups[-1]['top']) <= 2.5 and x0 - groups[-1]['x1'] <= 1.8:
            groups[-1]['text'] += text
            groups[-1]['x1'] = max(groups[-1]['x1'], x1)
        else:
            groups.append({'text': text, 'top': top, 'x0': x0, 'x1': x1})
    return groups


def parse_pdf(path):
    return parse_text(read_pdf_text(path))


def _pane_parts(thickness):
    """Glass, gap, glass... from '6/16/6 = 28 mm'. Overall millimetres are the last number."""
    body = str(thickness or '')
    formula = body.split('=', 1)[0]
    numbers = [int(float(part)) for part in re.findall(r'\d+(?:\.\d+)?', formula)]
    overall = None
    match = re.search(r'=\s*(\d+(?:\.\d+)?)', body)
    if match:
        overall = int(float(match.group(1)))
    if len(numbers) >= 3 and len(numbers) % 2 == 1:
        return numbers, overall
    return [], overall


def _lite_source(raw):
    text = _collapse(re.sub(r'\(\s*\d+\s*\)', ' ', raw or ''))
    parts = [part for part in text.split() if part.casefold() not in _MAKERS]
    return _collapse(' '.join(parts))


def _lites_from_name(name, panes):
    """Named lites in order. A leading overall thickness is not a lite."""
    detail = name
    if ',' in name and 'mm' in name.split(',', 1)[1].casefold():
        detail = name.split(',', 1)[1]
    detail = re.sub(r'\bTEMPERED\b', ' ', detail, flags=re.IGNORECASE)
    detail = re.sub(r'\bARGON\b', ' ', detail, flags=re.IGNORECASE)
    detail = _collapse(detail)
    glass_mm = panes[::2] if panes else []
    matches = list(_PANE_RE.finditer(detail))
    prefix = ''
    start = 0
    if matches and glass_mm:
        first = int(float(matches[0].group(1)))
        if first != glass_mm[0]:
            following = matches[1].start() if len(matches) > 1 else len(detail)
            prefix = _lite_source(detail[matches[0].end():following])
            start = 1
    lites = []
    usable = matches[start:]
    for index, match in enumerate(usable):
        thickness = int(float(match.group(1)))
        end = usable[index + 1].start() if index + 1 < len(usable) else len(detail)
        source = _lite_source(detail[match.end():end].strip(' +'))
        if index == 0 and prefix:
            source = _collapse(f'{prefix} {source}')
        if source:
            lites.append({'thickness_mm': thickness, 'source': source})
    if glass_mm and len(lites) != len(glass_mm):
        return [], prefix
    for lite, expected in zip(lites, glass_mm):
        lite['thickness_mm'] = expected
    return lites, prefix


def _blank_block():
    return {
        'name': '',
        'configuration': '',
        'thickness': '',
        'spacer_source': '',
        'panes': [],
        'gaps': [],
        'lites': [],
        'tempered': False,
        'argon': False,
        'lines': [],
        'sum_qty': None,
        'warnings': [],
    }


def parse_text(text):
    """Turn pdftotext -layout output, or rebuilt PDF lines, into blocks and rows."""
    raw_lines = [line.rstrip() for line in str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    lines = [_collapse(line) for line in raw_lines]
    if not any('glass/panel order' in line.casefold() for line in lines):
        raise OrgadataImportError(
            'This file is not an ORGADATA GLASS ORDER. The PDF must contain the heading Glass/Panel Order.'
        )
    document = {
        'sender': '',
        'reference_date': '',
        'project': '',
        'po': '',
        'contact': '',
        'blocks': [],
        'sum_quantity': None,
        'warnings': [],
    }
    for line in lines:
        if not document['sender'] and ' / ' in line and 'project name' not in line.casefold():
            document['sender'] = _collapse(line.split(' / ', 1)[0])
        date_match = _DATE_RE.match(line)
        if date_match:
            document['reference_date'] = date_match.group(1)
        project_match = _PROJECT_RE.match(line)
        if project_match:
            document['project'] = _collapse(project_match.group('project'))
            document['po'] = project_match.group('po')
        contact_match = _CONTACT_RE.match(line)
        if contact_match:
            document['contact'] = _collapse(contact_match.group('name'))
        sum_match = _SUM_QTY_RE.search(line)
        if sum_match:
            document['sum_quantity'] = int(sum_match.group('qty'))

    block = None
    collecting_name = False
    for line in lines:
        if not line or _PAGE_RE.match(line):
            continue
        if line.casefold() == 'glass':
            if block and (block['lines'] or block['name']):
                document['blocks'].append(block)
                block = _blank_block()
            elif block is None:
                block = _blank_block()
            collecting_name = False
            continue
        if line.lower().startswith('name:'):
            if block and (block['lines'] or block['name']):
                document['blocks'].append(block)
                block = _blank_block()
            elif block is None:
                block = _blank_block()
            block['name'] = _collapse(line.split(':', 1)[1])
            collecting_name = True
            continue
        if block is None:
            continue
        if line.lower().startswith('configuration:'):
            collecting_name = False
            block['configuration'] = _collapse(line.split(':', 1)[1])
            continue
        if line.lower().startswith('thickness:'):
            collecting_name = False
            match = _THICKNESS_RE.match(line)
            block['thickness'] = _collapse(match.group('body') if match else line.split(':', 1)[1])
            if match and match.group('spacer'):
                block['spacer_source'] = _collapse(match.group('spacer'))
            continue
        if line.lower().startswith('spacer:') and not block['spacer_source']:
            block['spacer_source'] = _collapse(line.split(':', 1)[1])
            continue
        if collecting_name and not line.lower().startswith('position'):
            block['name'] = _collapse(f"{block['name']} {line}")
            continue
        row_match = _ROW_RE.match(line)
        if row_match:
            collecting_name = False
            block['lines'].append({
                'floor': _floor(row_match.group('floor')),
                'qty': int(row_match.group('qty')),
                'width_mm': _number(row_match.group('width')),
                'height_mm': _number(row_match.group('height')),
            })
            continue
        sum_match = _SUM_RE.match(line)
        if sum_match:
            block['sum_qty'] = int(sum_match.group('qty'))
    if block and (block['lines'] or block['name']):
        document['blocks'].append(block)
    if not document['blocks']:
        raise OrgadataImportError('The Glass/Panel Order has no glass blocks.')

    line_number = 1
    total_qty = 0
    for block_index, block in enumerate(document['blocks']):
        panes, overall = _pane_parts(block['thickness'])
        block['gaps'] = panes[1::2]
        folded = block['name'].casefold()
        block['tempered'] = bool(re.search(r'\btempered\b', folded))
        block['argon'] = bool(re.search(r'\bargon\b', folded))
        lites, _prefix = _lites_from_name(block['name'], panes)
        if panes and not lites:
            block['warnings'].append('The glass name could not be split into the thicknesses on this block.')
            lites = [{'thickness_mm': mm, 'source': block['name']} for mm in panes[::2]]
        block['lites'] = lites
        if not block['lines']:
            block['warnings'].append('This glass block has no size rows.')
        got = sum(row['qty'] for row in block['lines'])
        total_qty += got
        if block['sum_qty'] is not None and got != block['sum_qty']:
            block['warnings'].append(
                f'Quantity {got} does not match the Sum: row ({block["sum_qty"]}).'
            )
        for row in block['lines']:
            row['line_number'] = line_number
            row['block'] = block_index
            line_number += 1
        if overall and panes and sum(panes) != overall:
            block['warnings'].append(
                f'Thickness parts add up to {sum(panes)} mm, not {overall} mm.'
            )
    if document['sum_quantity'] is not None and total_qty != document['sum_quantity']:
        document['warnings'].append(
            f'Quantity {total_qty} does not match Sum Quantity: {document["sum_quantity"]} Pcs.'
        )
    return document


def _thickness_of(name):
    match = _MM_VALUE_RE.search(str(name or ''))
    return int(match.group(1)) if match else None


def _phrase(name):
    text = _norm(name)
    text = re.sub(r'\b(jumbo|large|sqf)\b', ' ', text)
    return _collapse(text)


def _clear_product(thickness, names):
    hits = []
    for name in names:
        if _thickness_of(name) != thickness:
            continue
        if _phrase(name) == 'clear glass':
            hits.append(name)
    plain = [name for name in hits if 'jumbo' not in name.casefold() and 'large' not in name.casefold()]
    if len(plain) == 1:
        return plain[0]
    if len(hits) == 1:
        return hits[0]
    return ''


def _token_product(source, thickness, names):
    tokens = [
        token for token in _norm(source).split()
        if token not in _SKIP_TOKENS and token not in _MAKERS and not token.isdigit()
    ]
    if not tokens:
        return ''
    hits = []
    for name in names:
        if _thickness_of(name) != thickness:
            continue
        if 'jumbo' in name.casefold() or ' large' in name.casefold():
            continue
        phrase = _phrase(name)
        if all(token in phrase for token in tokens):
            hits.append(name)
    if len(hits) == 1:
        return hits[0]
    wanted = ' '.join(tokens)
    exact = [name for name in hits if _phrase(name) == wanted]
    if len(exact) == 1:
        return exact[0]
    return ''


def glass_alias_key(source):
    return _norm(source)


def spacer_alias_key(source, gap):
    return _norm(f'{source} {gap}')


def _family_at(product, thickness, names):
    from quoting import glass_family
    family = glass_family(product)
    hits = [
        name for name in names
        if glass_family(name) == family and _thickness_of(name) == thickness
        and 'jumbo' not in name.casefold() and ' large' not in name.casefold()
    ]
    if len(hits) == 1:
        return hits[0]
    return ''


def match_glass(source, thickness, names, aliases):
    """One ERP glass, or blank when the source is not a confident price-list match."""
    saved = (aliases or {}).get(glass_alias_key(source)) or ''
    if saved:
        known = next((name for name in names if name.casefold() == str(saved).casefold()), '')
        if known and _thickness_of(known) == thickness:
            return known
        if known:
            moved = _family_at(known, thickness, names)
            if moved:
                return moved
    text = _norm(source)
    text = _collapse(' '.join(part for part in text.split() if part not in _MAKERS))
    if text in _CLEAR:
        return _clear_product(thickness, names)
    return _token_product(source, thickness, names)


def parse_igu_description(text):
    """Split a plus-joined IGU name. A plain glass name returns None.

    '28 - 4mmSolarban60+20mmSuperSpacer with Argon(Polysulfide)+4mmSolarCoat'
    is two 4 mm lites, a 20 mm SuperSpacer, and argon. A triple pane is
    glass, spacer, glass, spacer, glass. The leading overall millimetres
    are not a lite.
    """
    raw = _collapse(text)
    if not raw or '+' not in raw:
        return None
    from production import makeup_from_product_name
    makeup = makeup_from_product_name(raw)
    if not makeup or makeup.count('+') < 2:
        return None
    parts = [part for part in makeup.split('+') if part]
    if len(parts) < 3 or len(parts) % 2 == 0:
        return None
    parsed = []
    for part in parts:
        match = re.match(r'(\d+(?:\.\d+)?)\s*mm\s*(.+)$', part, re.IGNORECASE)
        if not match:
            return None
        body = _collapse(match.group(2))
        if not body:
            return None
        parsed.append((int(float(match.group(1))), body))
    lites = []
    spacers = []
    argon = False
    tempered = False
    for index, (mm, body) in enumerate(parsed):
        if index % 2 == 0:
            if re.search(r'\btempered\b', body, re.IGNORECASE):
                tempered = True
            source = _lite_source(re.sub(r'\btempered\b', ' ', body, flags=re.IGNORECASE))
            if not source:
                return None
            lites.append({'thickness_mm': mm, 'source': source})
            continue
        if re.search(r'\bargon\b', body, re.IGNORECASE):
            argon = True
        note = ''
        found = re.search(r'\(([^)]+)\)', body)
        if found:
            note = _collapse(found.group(1))
        source = re.split(r'\bwith\b', body, maxsplit=1, flags=re.IGNORECASE)[0]
        source = _collapse(re.sub(r'\([^)]*\)', ' ', source))
        if not source:
            return None
        spacers.append({'gap_mm': mm, 'source': source, 'sealant_note': note})
    if len(lites) < 2 or len(spacers) != len(lites) - 1:
        return None
    return {'lites': lites, 'spacers': spacers, 'argon': argon, 'tempered': tempered}


def match_igu_description(text, glass_names, spacer_names, aliases=None):
    """Map an IGU description with the same glass and spacer matchers as a Glass/Panel Order."""
    parsed = parse_igu_description(text)
    if not parsed:
        return None
    aliases = aliases or {}
    glass_aliases = aliases.get('glass') or {}
    spacer_aliases = aliases.get('spacer') or {}
    lites = []
    spacers = []
    warnings = []
    for lite in parsed['lites']:
        product = match_glass(lite['source'], lite['thickness_mm'], glass_names, glass_aliases)
        lites.append({
            'source': lite['source'],
            'thickness_mm': lite['thickness_mm'],
            'product': product,
            'temper': 'yes' if parsed['tempered'] else 'n/a',
            'matched': bool(product),
        })
        if not product:
            warnings.append(f'Choose an ERP glass for {lite["source"]}.')
    for spacer in parsed['spacers']:
        product = match_spacer(spacer['source'], spacer['gap_mm'], spacer_names, spacer_aliases)
        spacers.append({
            'source': spacer['source'],
            'gap_mm': spacer['gap_mm'],
            'product': product,
            'matched': bool(product),
            'sealant_note': spacer.get('sealant_note') or '',
        })
        if not product:
            warnings.append(f'Choose an ERP spacer for {spacer["source"]} {spacer["gap_mm"]} mm.')
    return {
        'igu': True,
        'argon': parsed['argon'],
        'tempered': parsed['tempered'],
        'lites': lites,
        'spacers': spacers,
        'warnings': warnings,
        'matched': not warnings,
    }


def match_spacer(source, gap, names, aliases):
    saved = (aliases or {}).get(spacer_alias_key(source, gap)) or ''
    if saved:
        known = next((name for name in names if name.casefold() == str(saved).casefold()), '')
        if known and _thickness_of(known) == gap:
            return known
    folded = _norm(source)
    if 'technoform' in folded or folded.startswith('techno'):
        kind = 'techno'
    elif 'superspacer' in folded or 'super spacer' in folded:
        kind = 'super'
    else:
        return ''
    hits = []
    for name in names:
        if _thickness_of(name) != gap:
            continue
        lowered = name.casefold()
        if kind == 'techno' and 'techno' in lowered:
            hits.append(name)
        if kind == 'super' and 'super' in lowered:
            hits.append(name)
    if len(hits) == 1:
        return hits[0]
    return ''


def map_document(document, glass_names, spacer_names, aliases=None):
    """Fill ERP products on each block. Unmatched lites stay blank."""
    aliases = aliases or {'glass': {}, 'spacer': {}}
    mapped = []
    for block in document.get('blocks') or []:
        item = dict(block)
        item['warnings'] = list(block.get('warnings') or [])
        lites = []
        for lite in block.get('lites') or []:
            product = match_glass(lite.get('source'), lite.get('thickness_mm'), glass_names, aliases.get('glass'))
            lites.append({
                'source': lite.get('source') or '',
                'thickness_mm': lite.get('thickness_mm'),
                'product': product,
                'temper': 'yes' if block.get('tempered') else 'n/a',
                'matched': bool(product),
            })
            if not product:
                item['warnings'].append(f'Choose an ERP glass for {lite.get("source") or "this lite"}.')
        spacers = []
        gaps = list(block.get('gaps') or [])
        for index, gap in enumerate(gaps):
            product = match_spacer(block.get('spacer_source'), gap, spacer_names, aliases.get('spacer'))
            spacers.append({
                'source': block.get('spacer_source') or '',
                'gap_mm': gap,
                'product': product,
                'matched': bool(product),
            })
            if not product and block.get('spacer_source'):
                item['warnings'].append(
                    f'Choose an ERP spacer for {block.get("spacer_source")} {gap} mm.'
                )
        item['lites'] = lites
        item['spacers'] = spacers
        item['lines'] = [dict(line) for line in block.get('lines') or []]
        mapped.append(item)
    output = dict(document)
    output['blocks'] = mapped
    return output


def customer_matches(sender, customer_id, customer):
    """Open Windows and HighQ match the company name the ERP already uses."""
    key = _norm(sender)
    name = _norm((customer or {}).get('name'))
    ident = _norm(customer_id)
    if key.startswith('highq'):
        return name.startswith('highq') or ident.startswith('highq')
    if key.startswith('open windows') or key.startswith('openwindows'):
        return name.startswith('open windows') or name.startswith('openwindows') or ident.startswith('open windows')
    if not key:
        return False
    return name == key or name.startswith(key) or ident == key


def matching_customers(sender, customers):
    found = []
    for customer_id, customer in (customers or {}).items():
        if customer_matches(sender, customer_id, customer):
            found.append(customer_id)
    return found
