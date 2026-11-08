"""
ORA Glass ERP - Online Quoting & Pricing System
"""
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash, send_file
from functools import wraps
import openpyxl
from datetime import date
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
    'admin': {'password': hashlib.sha256('admin123'.encode()).hexdigest(), 'role': 'admin', 'name': 'Administrator'},
    'sales': {'password': hashlib.sha256('sales123'.encode()).hexdigest(), 'role': 'sales', 'name': 'Sales User'},
    'production': {'password': hashlib.sha256('prod123'.encode()).hexdigest(), 'role': 'production', 'name': 'Production User'},
    'customer': {'password': hashlib.sha256('customer123'.encode()).hexdigest(), 'role': 'customer', 'name': 'Customer User'}
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
PRICING = [
    {'name': 'Clear Glass 4mm', 'sheet_size': '130"x84"', 'base_price': 1.1232, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 5mm', 'sheet_size': '144"x84"', 'base_price': 1.4391, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 6mm', 'sheet_size': '144"x96"', 'base_price': 1.66725, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 6mm Jumbo', 'sheet_size': '204"x130"', 'base_price': 2.38875, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 8mm', 'sheet_size': '144"x96"', 'base_price': 2.3814, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 8mm Jumbo', 'sheet_size': '204"x130"', 'base_price': 3.7044, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 10mm', 'sheet_size': '144"x96"', 'base_price': 3.0051, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 10mm Jumbo', 'sheet_size': '204"x130"', 'base_price': 4.3953, 'temperable': True, 'annealed': True},
    {'name': 'LoE 180 4mm', 'sheet_size': '144"x84"', 'base_price': 1.56702, 'temperable': False, 'annealed': True},
    {'name': 'LoE 180 6mm', 'sheet_size': '144"x96"', 'base_price': 2.37742, 'temperable': False, 'annealed': True},
    {'name': 'LoE 366 4mm', 'sheet_size': '144"x96"', 'base_price': 2.12435, 'temperable': False, 'annealed': True},
    {'name': 'LoE 366 6mm', 'sheet_size': '144"x96"', 'base_price': 2.94635, 'temperable': False, 'annealed': True},
    {'name': 'LoE Q-180 4mm', 'sheet_size': '144"x96"', 'base_price': 2.20749, 'temperable': True, 'annealed': False},
    {'name': 'LoE Q-366 6mm', 'sheet_size': '144"x96"', 'base_price': 3.63745, 'temperable': True, 'annealed': False},
    {'name': 'Solarban60 4mm', 'sheet_size': '130"x96"', 'base_price': 2.22495, 'temperable': False, 'annealed': True},
    {'name': 'Solarban60 6mm', 'sheet_size': '144"x100"', 'base_price': 3.8493, 'temperable': False, 'annealed': True},
    {'name': 'SolarCoat 4mm', 'sheet_size': '144"x84"', 'base_price': 2.1762, 'temperable': True, 'annealed': True},
    {'name': 'SolarCoat 6mm', 'sheet_size': '144"x96"', 'base_price': 3.28185, 'temperable': True, 'annealed': True},
    {'name': 'Superspacer 10mm / sqf', 'base_price': 0.727776, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 11mm / sqf', 'base_price': 0.795207, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 12mm / sqf', 'base_price': 0.802788, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 14mm / sqf', 'base_price': 0.881657, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 16mm / sqf', 'base_price': 0.958531, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 18mm / sqf', 'base_price': 0.967974, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 20mm / sqf', 'base_price': 1.123584, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 11mm / sqf', 'base_price': 0.70, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 12mm / sqf', 'base_price': 0.74, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 14mm / sqf', 'base_price': 0.82, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 16mm / sqf', 'base_price': 0.90, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  10mm Polyurethane', 'base_price': 0.1483, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  10mm Silicone', 'base_price': 0.1796, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  11mm Polyurethane', 'base_price': 0.1624, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  11mm Silicone', 'base_price': 0.1967, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  12mm Polyurethane', 'base_price': 0.1695, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  12mm Silicone', 'base_price': 0.2052, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  14mm Polyurethane', 'base_price': 0.1977, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  14mm Silicone', 'base_price': 0.2394, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  16mm Polyurethane', 'base_price': 0.2260, 'temperable': False, 'annealed': False},
    {'name': 'Sealant -  16mm Silicone', 'base_price': 0.2736, 'temperable': False, 'annealed': False},
]

GLASS = [p for p in PRICING if 'mm' in p['name'].lower() and not any(x in p['name'].lower() for x in ['spacer', 'sealant', 'technoform'])]
SPACERS = [p for p in PRICING if 'spacer' in p['name'].lower() or 'technoform' in p['name'].lower()]
SEALANTS = [p for p in PRICING if 'sealant' in p['name'].lower()]

TIER_MARKUPS = {'T1': 0.30, 'T2': 0.40, 'T3': 0.60, 'T4': 0.75}
SPACER_MARKUPS = {'T1': 0.20, 'T2': 0.40, 'T3': 0.60, 'T4': 0.75}
TEMPER_PRICE = 0.65

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
                total += item['base_price'] * (1 + glass_markup)
                if data.get(key.replace('glass', 'temper')) == 'yes':
                    total += TEMPER_PRICE
    
    for key in ['spacer1', 'spacer2']:
        name = data.get(key, '')
        if name and name != 'n/a':
            item = next((p for p in PRICING if p['name'] == name), None)
            if item:
                total += item['base_price'] * (1 + spacer_markup)
    
    for key in ['sealant1', 'sealant2']:
        name = data.get(key, '')
        if name and name != 'n/a':
            item = next((p for p in PRICING if p['name'] == name), None)
            if item:
                total += item['base_price'] * (1 + spacer_markup)
    
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
    
    header_data = [[
        logo,
        Paragraph('<b>Quotation</b><br/><font size="8">2312 Leatherwood Pl.<br/>Lynden, WA 98264, USA</font>', title_style),
        Paragraph(f'<font size="8"><b>Date:</b> {date.today().strftime("%m/%d/%Y")}</font>', small_style)
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
        total_usd_sqf = 0
        total_cad_sqf = 0
        total_usd_pc = 0
        total_cad_pc = 0
        total_usd = 0
        for i, item in enumerate(items, 1):
            price_sqf = float(item.get('price_sqf', 0))
            price_pc = float(item.get('price_pc', 0))
            item_total = float(item.get('total', 0))
            sqf = float(item.get('sqf', 0))
            pcs = int(item.get('pcs', 1))
            total_sqf += sqf
            total_pcs += pcs
            total_usd_sqf += price_sqf
            total_cad_sqf += price_sqf * 1.4
            total_usd_pc += price_pc
            total_cad_pc += price_pc * 1.4
            total_usd += item_total
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
                Paragraph(f'<b>${total_usd*1.4:.2f}</b>', total_style)
            ])
            col_widths = [0.4*inch, 3.5*inch, 0.7*inch, 0.9*inch, 0.9*inch, 1*inch]
        else:
            table_data.append([
                Paragraph('', cell_style),
                Paragraph(f'<b>TOTAL ({total_pcs} pcs)</b>', total_style),
                Paragraph(f'<b>{total_sqf:.2f}</b>', total_style),
                Paragraph('', cell_style),
                Paragraph('', cell_style),
                Paragraph(f'<b>${total_usd:.2f}</b>', total_style)
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

@app.route('/users')
def users():
    return render_template('users.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
