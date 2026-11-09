"""
ORA Glass ERP - Online Quoting & Pricing System
"""
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash, send_file
from functools import wraps
import openpyxl
from datetime import date, datetime
from zoneinfo import ZoneInfo
import json
import os
import hashlib
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'ora-glass-erp-2026-secret'

ROLES = {
    'admin': {'name': 'Administrator', 'modules': ['quotes', 'quote-history', 'orders', 'customers', 'invoices', 'financial', 'inventory', 'pricing', 'users']},
    'sales': {'name': 'Sales', 'modules': ['quotes', 'quote-history', 'orders', 'customers', 'invoices']},
    'production': {'name': 'Production', 'modules': ['orders', 'inventory']},
    'viewer': {'name': 'Viewer', 'modules': ['quotes', 'quote-history', 'orders']},
    'customer': {'name': 'Customer', 'modules': ['quotes', 'quote-history', 'orders', 'invoices', 'financial']}
}

USERS = {
    'admin': {'password': hashlib.sha256('admin123'.encode()).hexdigest(), 'role': 'admin', 'name': 'Administrator', 'company': None},
    'sales': {'password': hashlib.sha256('sales123'.encode()).hexdigest(), 'role': 'sales', 'name': 'Sales User', 'company': None},
    'production': {'password': hashlib.sha256('prod123'.encode()).hexdigest(), 'role': 'production', 'name': 'Production User', 'company': None}
}

import json
QUOTES_FILE = 'quotes.json'
try:
    with open(QUOTES_FILE, 'r') as f:
        QUOTES = json.load(f)
except:
    QUOTES = []

def save_quotes():
    with open(QUOTES_FILE, 'w') as f:
        json.dump(QUOTES, f)

CUSTOMERS = {
    'demo': {'name': 'Demo Company', 'address': '123 Main St', 'city': 'Seattle', 'state': 'WA', 'zip': '98101', 'phone': '555-1234', 'email': 'demo@company.com', 'credit_approved': True, 'payment_terms': 'Net 30', 'tier': 'T2', 'status': 'active'}
}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def module_required(module):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user' not in session:
                return redirect(url_for('login'))
            user = USERS.get(session['user'])
            if not user or module not in ROLES.get(user['role'], {}).get('modules', []):
                flash('Access denied.', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator

# Pricing data
def load_pricing():
    try:
        wb = openpyxl.load_workbook('data/pricing_base.xlsx', data_only=True)
        ws = wb['pricelist']
        pricing = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[0] and row[8]:
                name = str(row[0])
                item = {
                    'thickness': str(row[1]) if row[1] else '',
                    'sheet_size': f'{row[2]}"x{row[3]}"' if row[2] and row[3] else '',
                    'base_price': float(row[8]) if row[8] else 0,
                    'temperable': True,
                    'annealed': True
                }
                if '180/270' in name:
                    pricing.append({**item, 'name': name.replace('180/270', '180')})
                    pricing.append({**item, 'name': name.replace('180/270', '270')})
                else:
                    pricing.append({**item, 'name': name})
        return pricing
    except Exception as e:
        print(f'Pricing load error: {e}')
        return [
            # Clear Glass
            {'name': 'Clear Glass 4mm', 'sheet_size': '130"x84"', 'base_price': 1.12, 'temperable': True, 'annealed': True},
            {'name': 'Clear Glass 5mm', 'sheet_size': '144"x84"', 'base_price': 1.44, 'temperable': True, 'annealed': True},
            {'name': 'Clear Glass 6mm', 'sheet_size': '144"x84"', 'base_price': 1.67, 'temperable': True, 'annealed': True},
            {'name': 'Clear Glass 6mm Jumbo', 'sheet_size': '144"x100"', 'base_price': 2.39, 'temperable': True, 'annealed': True},
            {'name': 'Clear Glass 8mm', 'sheet_size': '144"x84"', 'base_price': 2.38, 'temperable': True, 'annealed': True},
            {'name': 'Clear Glass 8mm Jumbo', 'sheet_size': '144"x100"', 'base_price': 3.70, 'temperable': True, 'annealed': True},
            {'name': 'Clear Glass 10mm', 'sheet_size': '144"x84"', 'base_price': 3.01, 'temperable': True, 'annealed': True},
            {'name': 'Clear Glass 10mm Jumbo', 'sheet_size': '144"x100"', 'base_price': 4.40, 'temperable': True, 'annealed': True},
            {'name': 'Clear Glass 12mm', 'sheet_size': '144"x84"', 'base_price': 3.44, 'temperable': True, 'annealed': True},
            # Bronze
            {'name': 'Bronze 4mm', 'sheet_size': '130"x84"', 'base_price': 2.22, 'temperable': True, 'annealed': True},
            {'name': 'Bronze 6mm', 'sheet_size': '130"x84"', 'base_price': 3.49, 'temperable': True, 'annealed': True},
            # Sandblast
            {'name': 'Sandblast 4mm', 'sheet_size': '130"x84"', 'base_price': 1.84, 'temperable': True, 'annealed': True},
            {'name': 'Sandblast 6mm', 'sheet_size': '130"x84"', 'base_price': 2.88, 'temperable': True, 'annealed': True},
            # SolarCoat
            {'name': 'SolarCoat 4mm', 'sheet_size': '130"x84"', 'base_price': 2.18, 'temperable': True, 'annealed': True},
            {'name': 'SolarCoat 6mm', 'sheet_size': '130"x84"', 'base_price': 3.28, 'temperable': True, 'annealed': True},
            # Solarban
            {'name': 'Solarban60 4mm', 'sheet_size': '130"x84"', 'base_price': 2.22, 'temperable': True, 'annealed': False},
            {'name': 'Solarban60 6mm', 'sheet_size': '130"x84"', 'base_price': 3.85, 'temperable': True, 'annealed': False},
            {'name': 'Solarban60 VT 4mm', 'sheet_size': '130"x84"', 'base_price': 2.50, 'temperable': True, 'annealed': False},
            {'name': 'Solarban60 VT 6mm', 'sheet_size': '130"x84"', 'base_price': 4.27, 'temperable': True, 'annealed': False},
            # LoE
            {'name': 'LoE 180 4mm', 'sheet_size': '130"x84"', 'base_price': 1.57, 'temperable': True, 'annealed': False},
            {'name': 'LoE 180 6mm', 'sheet_size': '130"x84"', 'base_price': 2.38, 'temperable': True, 'annealed': False},
            {'name': 'LoE 270 4mm', 'sheet_size': '130"x84"', 'base_price': 1.57, 'temperable': True, 'annealed': False},
            {'name': 'LoE 270 6mm', 'sheet_size': '130"x84"', 'base_price': 2.38, 'temperable': True, 'annealed': False},
            {'name': 'LoE 366 4mm', 'sheet_size': '130"x84"', 'base_price': 2.12, 'temperable': True, 'annealed': False},
            {'name': 'LoE 366 6mm', 'sheet_size': '130"x84"', 'base_price': 2.95, 'temperable': True, 'annealed': False},
            {'name': 'LoE Q-180 4mm', 'sheet_size': '130"x84"', 'base_price': 2.21, 'temperable': True, 'annealed': False},
            {'name': 'LoE Q-180 6mm', 'sheet_size': '130"x84"', 'base_price': 2.88, 'temperable': True, 'annealed': False},
            {'name': 'LoE Q-270 4mm', 'sheet_size': '130"x84"', 'base_price': 2.21, 'temperable': True, 'annealed': False},
            {'name': 'LoE Q-270 6mm', 'sheet_size': '130"x84"', 'base_price': 2.88, 'temperable': True, 'annealed': False},
            {'name': 'LoE Q-366 4mm', 'sheet_size': '130"x84"', 'base_price': 3.00, 'temperable': True, 'annealed': False},
            {'name': 'LoE Q-366 6mm', 'sheet_size': '130"x84"', 'base_price': 3.64, 'temperable': True, 'annealed': False},
            {'name': 'LoE i89 4mm', 'sheet_size': '130"x84"', 'base_price': 3.36, 'temperable': True, 'annealed': False},
            {'name': 'LoE i89 6mm', 'sheet_size': '130"x84"', 'base_price': 3.85, 'temperable': True, 'annealed': False},
            # Planitherm
            {'name': 'Planitherm 4mm', 'sheet_size': '130"x84"', 'base_price': 2.75, 'temperable': True, 'annealed': False},
            {'name': 'Planitherm 6mm', 'sheet_size': '130"x84"', 'base_price': 3.81, 'temperable': True, 'annealed': False},
            {'name': 'Planitherm II EZP 4mm', 'sheet_size': '130"x84"', 'base_price': 2.95, 'temperable': True, 'annealed': False},
            {'name': 'Planitherm II EZP 6mm', 'sheet_size': '130"x84"', 'base_price': 3.81, 'temperable': True, 'annealed': False},
            {'name': 'Planitherm Lux 4mm', 'sheet_size': '130"x84"', 'base_price': 2.61, 'temperable': True, 'annealed': False},
            {'name': 'Planitherm Lux 6mm', 'sheet_size': '130"x84"', 'base_price': 3.51, 'temperable': True, 'annealed': False},
            # SKN
            {'name': 'SKN176 4mm', 'sheet_size': '130"x84"', 'base_price': 3.35, 'temperable': True, 'annealed': False},
            {'name': 'SKN176 6mm', 'sheet_size': '130"x84"', 'base_price': 4.50, 'temperable': True, 'annealed': False},
            {'name': 'SKN176II EZP 4mm', 'sheet_size': '130"x84"', 'base_price': 3.48, 'temperable': True, 'annealed': False},
            {'name': 'SKN176II EZP 6mm', 'sheet_size': '130"x84"', 'base_price': 4.63, 'temperable': True, 'annealed': False},
            # Spacers
            {'name': 'Superspacer 10mm / sqf', 'sheet_size': '', 'base_price': 0.73, 'temperable': False, 'annealed': False},
            {'name': 'Superspacer 11mm / sqf', 'sheet_size': '', 'base_price': 0.75, 'temperable': False, 'annealed': False},
            {'name': 'Superspacer 12mm / sqf', 'sheet_size': '', 'base_price': 0.80, 'temperable': False, 'annealed': False},
            {'name': 'Superspacer 14mm / sqf', 'sheet_size': '', 'base_price': 0.85, 'temperable': False, 'annealed': False},
            {'name': 'Superspacer 16mm / sqf', 'sheet_size': '', 'base_price': 0.90, 'temperable': False, 'annealed': False},
            {'name': 'Superspacer 18mm / sqf', 'sheet_size': '', 'base_price': 0.95, 'temperable': False, 'annealed': False},
            {'name': 'Superspacer 20mm / sqf', 'sheet_size': '', 'base_price': 1.00, 'temperable': False, 'annealed': False},
            {'name': 'Technoform 11mm / sqf', 'sheet_size': '', 'base_price': 0.45, 'temperable': False, 'annealed': False},
            {'name': 'Technoform 12mm / sqf', 'sheet_size': '', 'base_price': 0.47, 'temperable': False, 'annealed': False},
            {'name': 'Technoform 14mm / sqf', 'sheet_size': '', 'base_price': 0.51, 'temperable': False, 'annealed': False},
            {'name': 'Technoform 16mm / sqf', 'sheet_size': '', 'base_price': 0.55, 'temperable': False, 'annealed': False},
            {'name': 'Technoform 18mm / sqf', 'sheet_size': '', 'base_price': 0.59, 'temperable': False, 'annealed': False},
            {'name': 'Technoform 20mm / sqf', 'sheet_size': '', 'base_price': 0.63, 'temperable': False, 'annealed': False},
            # Sealants
            {'name': 'Sealant - 10mm Polyurethane', 'sheet_size': '', 'base_price': 0.15, 'temperable': False, 'annealed': False},
            {'name': 'Sealant - 12mm Polyurethane', 'sheet_size': '', 'base_price': 0.17, 'temperable': False, 'annealed': False},
            {'name': 'Sealant - 16mm Polyurethane', 'sheet_size': '', 'base_price': 0.20, 'temperable': False, 'annealed': False},
            {'name': 'Sealant - 18mm Polyurethane', 'sheet_size': '', 'base_price': 0.22, 'temperable': False, 'annealed': False},
            {'name': 'Sealant - 20mm Polyurethane', 'sheet_size': '', 'base_price': 0.24, 'temperable': False, 'annealed': False},
            {'name': 'Sealant - 10mm Silicone', 'sheet_size': '', 'base_price': 0.18, 'temperable': False, 'annealed': False},
            {'name': 'Sealant - 12mm Silicone', 'sheet_size': '', 'base_price': 0.20, 'temperable': False, 'annealed': False},
            {'name': 'Sealant - 16mm Silicone', 'sheet_size': '', 'base_price': 0.23, 'temperable': False, 'annealed': False},
            {'name': 'Sealant - 18mm Silicone', 'sheet_size': '', 'base_price': 0.25, 'temperable': False, 'annealed': False},
            {'name': 'Sealant - 20mm Silicone', 'sheet_size': '', 'base_price': 0.27, 'temperable': False, 'annealed': False},
        ]

PRICING = load_pricing()

GLASS = [p for p in PRICING if 'mm' in p['name'].lower() and not any(x in p['name'].lower() for x in ['spacer', 'sealant', 'technoform'])]
SPACERS = [p for p in PRICING if 'spacer' in p['name'].lower() or 'technoform' in p['name'].lower()]
SEALANTS = [p for p in PRICING if 'sealant' in p['name'].lower()]

TIER_MARKUPS = {'T1': 0.30, 'T2': 0.40, 'T3': 0.60, 'T4': 0.75, 'T5': 4.25}
SPACER_MARKUPS = {'T1': 0.20, 'T2': 0.40, 'T3': 0.60, 'T4': 0.75, 'T5': 4.25}
TEMPER_PRICES = {'4mm': 0.50, '5mm': 0.55, '6mm': 0.65, '8mm': 0.75, '10mm': 0.85, '12mm': 0.95}

@app.route('/')
def index():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').lower()
        password = request.form.get('password', '')
        user = USERS.get(username)
        if user and user['password'] == hashlib.sha256(password.encode()).hexdigest():
            session['user'] = username
            return redirect(url_for('dashboard'))
        flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    user = USERS.get(session['user'], {})
    role = ROLES.get(user.get('role', ''), {})
    return render_template('dashboard.html', user=user, role_name=role.get('name', ''), modules=role.get('modules', []))

@app.route('/quotes')
@login_required
@module_required('quotes')
def quotes():
    return render_template('quotes.html', glass=GLASS, spacers=SPACERS, sealants=SEALANTS)

@app.route('/api/calculate', methods=['POST'])
@login_required
def calculate():
    data = request.json
    tier = data.get('tier', 'T2')
    glass_markup = TIER_MARKUPS.get(tier, 0.40)
    spacer_markup = SPACER_MARKUPS.get(tier, 0.40)
    
    total = 0
    for key in ['glass1', 'glass2', 'glass3']:
        name = data.get(key, '')
        if name and name != 'n/a':
            item = next((p for p in PRICING if p['name'] == name), None)
            if item:
                total += round(item['base_price'] * (1 + glass_markup), 2)
                if data.get(key.replace('glass', 'temper')) == 'yes':
                    thick = name.split()[-1] if name else ''
                    total += TEMPER_PRICES.get(thick, 0.65)
    
    for key in ['spacer1', 'spacer2']:
        name = data.get(key, '')
        if name and name != 'n/a':
            item = next((p for p in PRICING if p['name'] == name), None)
            if item:
                total += round(item['base_price'] * (1 + spacer_markup), 2)
    
    for key in ['sealant1', 'sealant2']:
        name = data.get(key, '')
        if name and name != 'n/a':
            item = next((p for p in PRICING if p['name'] == name), None)
            if item:
                total += round(item['base_price'] * (1 + spacer_markup), 2)
    
    return jsonify({'price_sqf': round(total, 2)})

@app.route('/api/export-quote', methods=['POST'])
@login_required
def export_quote():
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    
    data = request.json
    items = data.get('items', [])
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=0.4*inch, rightMargin=0.4*inch, topMargin=0.4*inch, bottomMargin=0.4*inch)
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle('Title', parent=styles['Normal'], fontSize=14, fontName='Helvetica-Bold', alignment=1)
    small_style = ParagraphStyle('Small', parent=styles['Normal'], fontSize=8, leading=10)
    cell_style = ParagraphStyle('Cell', parent=styles['Normal'], fontSize=7, leading=9)
    header_cell = ParagraphStyle('HeaderCell', parent=styles['Normal'], fontSize=7, fontName='Helvetica-Bold', textColor=colors.white)
    
    elements = []
    
    # Header with logo
    logo_path = 'logo.png' if os.path.exists('logo.png') else None
    if logo_path:
        logo = Image(logo_path, width=1.2*inch, height=0.8*inch)
    else:
        logo = Paragraph('<font color="#C41E3A" size="18"><b>ORA</b></font><br/><font size="10">GLASS</font>', small_style)
    
    project = data.get('project', '')
    project_line = f'<br/><b>Project:</b> {project}' if project else ''
    header_data = [[
        logo,
        Paragraph('<b>Quotation</b><br/><font size="8">2312 Leatherwood Pl.<br/>Lynden, WA 98264, USA</font>', title_style),
        Paragraph('<font size="8"><b>Date:</b> ' + date.today().strftime("%m/%d/%Y") + '<br/><b>Time:</b> ' + datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%H:%M") + ' PST' + project_line + '</font>', small_style)
    ]]
    header_table = Table(header_data, colWidths=[1.5*inch, 3*inch, 2.5*inch])
    header_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    elements.append(header_table)
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.black))
    elements.append(Spacer(1, 6))
    
    
    
    # Items table - check if per size data
    has_size = any(item.get('sqf') for item in items)
    currency = data.get('currency', 'usd')
    if has_size:
        if currency == 'cad':
            table_data = [[
                Paragraph('<b>ID</b>', header_cell),
                Paragraph('<b>ITEM DESCRIPTION</b>', header_cell),
                Paragraph('<b>SQF</b>', header_cell),
                Paragraph('<b>CAD/SQF</b>', header_cell),
                Paragraph('<b>CAD/PC</b>', header_cell),
                Paragraph('<b>TOTAL CAD</b>', header_cell)
            ]]
        else:
            table_data = [[
                Paragraph('<b>ID</b>', header_cell),
                Paragraph('<b>ITEM DESCRIPTION</b>', header_cell),
                Paragraph('<b>SQF</b>', header_cell),
                Paragraph('<b>USD/SQF</b>', header_cell),
                Paragraph('<b>USD/PC</b>', header_cell),
                Paragraph('<b>TOTAL USD</b>', header_cell)
            ]]
        total_sqf = 0
        total_pcs = 0
        total_line_sum = 0
        for i, item in enumerate(items, 1):
            price_sqf = float(item.get('price_sqf', 0))
            price_pc = float(item.get('price_pc', 0))
            sqf = float(item.get('sqf', 0))
            pcs = int(item.get('pcs', 1))
            total_sqf += sqf * pcs
            total_pcs += pcs
            total_line_sum += price_pc * pcs
            pcs = int(item.get('pcs', 1))
            sqf_per_pc = float(item.get('sqf', 0))
            total_sqf_line = sqf_per_pc * pcs
            price_pc = float(item.get('price_pc', 0))
            total_line = price_pc * pcs
            table_data.append([
                Paragraph(f'({i})', cell_style),
                Paragraph(item.get('name', ''), cell_style),
                Paragraph(f"{total_sqf_line:.2f}", cell_style),
                Paragraph(f"${price_sqf:.2f}", cell_style),
                Paragraph(f"${price_pc:.2f}", cell_style),
                Paragraph(f"${total_line:.2f}", cell_style)
            ])
        # Totals row
        total_style = ParagraphStyle('Total', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold')
        if currency == 'cad':
            table_data.append([
                Paragraph('', cell_style),
                Paragraph(f'<b>TOTAL ({total_pcs} pcs)</b>', total_style),
                Paragraph(f'<b>{total_sqf:.2f}</b>', total_style),
                Paragraph('', cell_style),
                Paragraph('', cell_style),
                Paragraph(f'<b>${total_line_sum*1.4:.2f}</b>', total_style)
            ])
            col_widths = [0.4*inch, 3.5*inch, 0.7*inch, 0.9*inch, 0.9*inch, 1*inch]
        else:
            table_data.append([
                Paragraph('', cell_style),
                Paragraph(f'<b>TOTAL ({total_pcs} pcs)</b>', total_style),
                Paragraph(f'<b>{total_sqf:.2f}</b>', total_style),
                Paragraph('', cell_style),
                Paragraph('', cell_style),
                Paragraph(f'<b>${total_line_sum:.2f}</b>', total_style)
            ])
            col_widths = [0.4*inch, 3.5*inch, 0.7*inch, 0.9*inch, 0.9*inch, 1*inch]
    else:
        curr_label = 'CAD/SQF' if currency == 'cad' else 'USD/SQF'
        table_data = [[
            Paragraph('<b>ID</b>', header_cell),
            Paragraph('<b>ITEM DESCRIPTION</b>', header_cell),
            Paragraph(f'<b>{curr_label}</b>', header_cell)
        ]]
        for i, item in enumerate(items, 1):
            price = item.get('price', '0.00')
            table_data.append([
                Paragraph(f'({i})', cell_style),
                Paragraph(item.get('name', ''), cell_style),
                Paragraph(f"${price}", cell_style)
            ])
        col_widths = [0.4*inch, 5.5*inch, 1.1*inch]
    
    items_table = Table(table_data, colWidths=col_widths)
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#C41E3A')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 20))
    
    elements.append(Spacer(1, 10))
    elements.append(Paragraph('<font size="7">Ora Glass Inc Terms And Conditions of Sale apply.</font>', small_style))
    
    doc.build(elements)
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name='ORA_Quote.pdf')

@app.route('/orders')
@login_required
@module_required('orders')
def orders():
    return render_template('module.html', module='Orders', user=session)

@app.route('/customers')
@login_required
@module_required('customers')
def customers():
    return render_template('module.html', module='Customers', user=session)

@app.route('/inventory')
@login_required
@module_required('inventory')
def inventory():
    return render_template('module.html', module='Inventory', user=session)

@app.route('/pricing')
@login_required
@module_required('pricing')
def pricing():
    return render_template('module.html', module='Pricing Admin', user=session)

@app.route('/users')
@login_required
@module_required('users')
def users():
    return render_template('module.html', module='Users', user=session)

@app.route('/quote-history')
def quote_history():
    return render_template('quote_history.html')

@app.route('/invoices')
def invoices():
    return render_template('invoices.html')

@app.route('/financial')
def financial():
    return render_template('financial.html')

# Customer API
@app.route('/api/customers', methods=['GET'])
@login_required
def get_customers():
    return jsonify([{'id': k, **v} for k, v in CUSTOMERS.items()])

@app.route('/api/customers', methods=['POST'])
@login_required
def add_customer():
    data = request.json
    cid = data.get('id', data['name'].lower().replace(' ', '_'))
    CUSTOMERS[cid] = {'name': data['name'], 'address': data.get('address', ''), 'city': data.get('city', ''), 'state': data.get('state', ''), 'zip': data.get('zip', ''), 'phone': data.get('phone', ''), 'email': data.get('email', ''), 'credit_approved': data.get('credit_approved', False), 'payment_terms': data.get('payment_terms', 'Net 30'), 'tier': data.get('tier', 'T2'), 'status': 'active'}
    return jsonify({'success': True, 'id': cid})

@app.route('/api/customers/<cid>', methods=['PUT'])
@login_required
def update_customer(cid):
    if cid in CUSTOMERS:
        CUSTOMERS[cid].update(request.json)
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/customers/<cid>', methods=['DELETE'])
@login_required
def delete_customer(cid):
    if cid in CUSTOMERS:
        del CUSTOMERS[cid]
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

# User API
@app.route('/api/users', methods=['GET'])
@login_required
def get_users():
    return jsonify([{'id': k, 'name': v['name'], 'role': v['role'], 'company': v.get('company')} for k, v in USERS.items()])

@app.route('/api/users', methods=['POST'])
@login_required
def add_user():
    data = request.json
    uid = data['email'].split('@')[0]
    USERS[uid] = {'password': hashlib.sha256(data['password'].encode()).hexdigest(), 'role': data.get('role', 'customer'), 'name': data['name'], 'company': data.get('company')}
    return jsonify({'success': True, 'id': uid})

@app.route('/api/users/<uid>', methods=['DELETE'])
@login_required
def delete_user(uid):
    if uid in USERS and uid != 'admin':
        del USERS[uid]
        return jsonify({'success': True})
    return jsonify({'error': 'Cannot delete'}), 400

@app.route('/api/quotes', methods=['POST'])
@login_required
def save_quote():
    data = request.json
    user_data = USERS.get(session.get('user'), {})
    company = user_data.get('company') or 'ORA'
    quote = {'id': len(QUOTES)+1, 'date': date.today().isoformat(), 'time': datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%H:%M'), 'user': session.get('user'), 'company': company, 'project': data.get('project', ''), 'items': data.get('items', []), 'currency': data.get('currency', 'usd'), 'type': data.get('type', 'sqf')}
    QUOTES.append(quote)
    save_quotes()
    return jsonify({'success': True, 'id': quote['id']})

@app.route('/api/quotes/clear', methods=['POST'])
@login_required
def clear_quotes():
    global QUOTES
    QUOTES = []
    save_quotes()
    return jsonify({'success': True})
    return jsonify({'success': True})

@app.route('/api/quotes', methods=['GET'])
@login_required
def get_quotes():
    user = session.get('user')
    role = USERS.get(user, {}).get('role')
    if role == 'admin':
        return jsonify(QUOTES)
    return jsonify([q for q in QUOTES if q['user'] == user])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
