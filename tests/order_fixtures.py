"""Sample customer order forms used by the import tests."""

import io

from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle


def inches_xlsx():
    book = Workbook()
    cover = book.active
    cover.title = 'Cover'
    cover['A1'] = 'Not the order'
    sheet = book.create_sheet('Orders')
    sheet.append(['Mark', 'Width', 'Height', 'Qty', 'Glass', 'Spacer'])
    sheet.append(['W1', '23 1/2', '35-1/4"', 2, 'Their Clear', 'Warm edge 12'])
    sheet.append(['W2', '24.5"', '36', 1, 'Clear Glass 6mm', None])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def mm_csv():
    text = 'Width,Height,Pcs,Description\n600,900,4,Bronze lite\n450,800,1,Clear Glass 4mm\n'
    return text.encode('utf-8')


def mm_pdf():
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    data = [
        ['Mark', 'Width', 'Height', 'Pcs', 'Glass'],
        ['A1', '700', '1000', '2', 'Patio glass'],
        ['A2', '500', '800', '1', 'Clear Glass 4mm'],
    ]
    table = Table(data)
    table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.4, colors.black),
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
    ]))
    doc.build([table])
    return buffer.getvalue()


def mm_xls():
    import xlwt
    book = xlwt.Workbook()
    sheet = book.add_sheet('Orders')
    for col, header in enumerate(['Width', 'Height', 'Pcs', 'Glass']):
        sheet.write(0, col, header)
    sheet.write(1, 0, 500)
    sheet.write(1, 1, 800)
    sheet.write(1, 2, 5)
    sheet.write(1, 3, 'Shop glass')
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
