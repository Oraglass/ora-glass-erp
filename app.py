"""
ORA Glass ERP - Online Quoting & Pricing System
With role-based access control
"""
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from functools import wraps
import openpyxl
from datetime import date
import json
import os
import hashlib

app = Flask(__name__)
app.secret_key = 'ora-glass-erp-2026-secret'

# User roles and permissions
ROLES = {
    'admin': {
        'name': 'Administrator',
        'modules': ['quotes', 'orders', 'customers', 'inventory', 'pricing', 'users', 'reports']
    },
    'sales': {
        'name': 'Sales',
        'modules': ['quotes', 'orders', 'customers']
    },
    'production': {
        'name': 'Production',
        'modules': ['orders', 'inventory']
    },
    'viewer': {
        'name': 'Viewer',
        'modules': ['quotes', 'orders']  # read-only
    },
    'customer': {
        'name': 'Customer',
        'modules': ['quotes', 'orders']  # view own quotes/orders only
    }
}

# Default users (in production, use a database)
USERS = {
    'admin': {
        'password': hashlib.sha256('admin123'.encode()).hexdigest(),
        'role': 'admin',
        'name': 'Administrator'
    },
    'sales': {
        'password': hashlib.sha256('sales123'.encode()).hexdigest(),
        'role': 'sales',
        'name': 'Sales User'
    },
    'production': {
        'password': hashlib.sha256('prod123'.encode()).hexdigest(),
        'role': 'production',
        'name': 'Production User'
    },
    'customer': {
        'password': hashlib.sha256('customer123'.encode()).hexdigest(),
        'role': 'customer',
        'name': 'Customer User'
    }
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
            if not user:
                return redirect(url_for('login'))
            role = ROLES.get(user['role'], {})
            if module not in role.get('modules', []):
                flash('Access denied. You do not have permission to access this module.', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator

# Load pricing
def load_pricing():
    try:
        wb = openpyxl.load_workbook('data/pricing_base.xlsx', data_only=True)
        ws = wb['pricelist']
        items = []
        for row in range(2, 110):
            name = ws.cell(row, 1).value
            if not name:
                continue
            items.append({
                'name': str(name).strip(),
                'thickness': str(ws.cell(row, 2).value or ''),
                'base_price': float(ws.cell(row, 9).value or 0),
                'temperable': str(ws.cell(row, 10).value or '').lower() == 'yes'
            })
        return items
    except:
        return []

PRICING = load_pricing()
if not PRICING:
    PRICING = [
        {'name': 'Clear Glass 4mm', 'thickness': '4mm', 'base_price': 1.1232, 'temperable': True, 'annealed': True},
        {'name': 'Clear Glass 5mm', 'thickness': '5mm', 'base_price': 1.4391, 'temperable': True, 'annealed': True},
        {'name': 'Clear Glass 6mm', 'thickness': '6mm', 'base_price': 1.66725, 'temperable': True, 'annealed': True},
        {'name': 'Clear Glass 6mm Large', 'thickness': '6mm', 'base_price': 2.38875, 'temperable': True, 'annealed': True},
        {'name': 'Clear Glass 8mm', 'thickness': '8mm', 'base_price': 2.3814, 'temperable': True, 'annealed': True},
        {'name': 'Clear Glass 8mm Large', 'thickness': '8mm', 'base_price': 3.7044, 'temperable': True, 'annealed': True},
        {'name': 'Clear Glass 10mm', 'thickness': '10mm', 'base_price': 3.0051, 'temperable': True, 'annealed': True},
        {'name': 'Clear Glass 10mm Large', 'thickness': '10mm', 'base_price': 4.3953, 'temperable': True, 'annealed': True},
        {'name': 'Clear Glass 12mm', 'thickness': '12mm', 'base_price': 3.4398, 'temperable': True, 'annealed': True},
        {'name': 'Clear Glass 12mm Large', 'thickness': '12mm', 'base_price': 5.6007, 'temperable': True, 'annealed': True},
        {'name': 'Bronze 4mm', 'thickness': '4mm', 'base_price': 2.22495, 'temperable': True, 'annealed': True},
        {'name': 'Bronze 6mm', 'thickness': '6mm', 'base_price': 3.49245, 'temperable': True, 'annealed': True},
        {'name': 'Sandblast 4mm', 'thickness': '4mm', 'base_price': 1.84275, 'temperable': True, 'annealed': True},
        {'name': 'Sandblast 6mm', 'thickness': '6mm', 'base_price': 2.8782, 'temperable': True, 'annealed': True},
        {'name': 'SolarCoat 4mm', 'thickness': '4mm', 'base_price': 2.1762, 'temperable': True, 'annealed': True},
        {'name': 'SolarCoat 5mm', 'thickness': '5mm', 'base_price': 2.7378, 'temperable': True, 'annealed': True},
        {'name': 'SolarCoat 6mm', 'thickness': '6mm', 'base_price': 3.28185, 'temperable': True, 'annealed': True},
        {'name': 'SolarCoat 8mm', 'thickness': '8mm', 'base_price': 4.7628, 'temperable': True, 'annealed': True},
        {'name': 'SolarCoat 10mm', 'thickness': '10mm', 'base_price': 6.7284, 'temperable': True, 'annealed': True},
        {'name': 'Solarban60 4mm', 'thickness': '4mm', 'base_price': 2.22495, 'temperable': False, 'annealed': True},
        {'name': 'Solarban60 5mm', 'thickness': '5mm', 'base_price': 3.8493, 'temperable': False, 'annealed': True},
        {'name': 'Solarban60 6mm', 'thickness': '6mm', 'base_price': 3.8493, 'temperable': False, 'annealed': True},
        {'name': 'Solarban60 VT 4mm', 'thickness': '4mm', 'base_price': 2.49795, 'temperable': True, 'annealed': False},
        {'name': 'Solarban60 VT 5mm', 'thickness': '5mm', 'base_price': 3.5217, 'temperable': True, 'annealed': False},
        {'name': 'Solarban60 VT 6mm', 'thickness': '6mm', 'base_price': 4.27245, 'temperable': True, 'annealed': False},
        {'name': 'LoE 180 4mm', 'thickness': '4mm', 'base_price': 1.56702, 'temperable': False, 'annealed': True},
        {'name': 'LoE 180 5mm', 'thickness': '5mm', 'base_price': 1.96328, 'temperable': False, 'annealed': True},
        {'name': 'LoE 180 6mm', 'thickness': '6mm', 'base_price': 2.37742, 'temperable': False, 'annealed': True},
        {'name': 'LoE 270 4mm', 'thickness': '4mm', 'base_price': 1.56702, 'temperable': False, 'annealed': True},
        {'name': 'LoE 270 5mm', 'thickness': '5mm', 'base_price': 1.96328, 'temperable': False, 'annealed': True},
        {'name': 'LoE 270 6mm', 'thickness': '6mm', 'base_price': 2.37742, 'temperable': False, 'annealed': True},
        {'name': 'LoE 366 4mm', 'thickness': '4mm', 'base_price': 2.12435, 'temperable': False, 'annealed': True},
        {'name': 'LoE 366 5mm', 'thickness': '5mm', 'base_price': 2.29129, 'temperable': False, 'annealed': True},
        {'name': 'LoE 366 6mm', 'thickness': '6mm', 'base_price': 2.94635, 'temperable': False, 'annealed': True},
        {'name': 'LoE Q-180 4mm', 'thickness': '4mm', 'base_price': 2.20749, 'temperable': True, 'annealed': False},
        {'name': 'LoE Q-180 5mm', 'thickness': '5mm', 'base_price': 2.52786, 'temperable': True, 'annealed': False},
        {'name': 'LoE Q-180 6mm', 'thickness': '6mm', 'base_price': 2.87538, 'temperable': True, 'annealed': False},
        {'name': 'LoE Q-270 4mm', 'thickness': '4mm', 'base_price': 2.20749, 'temperable': True, 'annealed': False},
        {'name': 'LoE Q-270 5mm', 'thickness': '5mm', 'base_price': 2.52786, 'temperable': True, 'annealed': False},
        {'name': 'LoE Q-270 6mm', 'thickness': '6mm', 'base_price': 2.87538, 'temperable': True, 'annealed': False},
        {'name': 'LoE Q-366 4mm', 'thickness': '4mm', 'base_price': 3.00095, 'temperable': True, 'annealed': False},
        {'name': 'LoE Q-366 5mm', 'thickness': '5mm', 'base_price': 3.18277, 'temperable': True, 'annealed': False},
        {'name': 'LoE Q-366 6mm', 'thickness': '6mm', 'base_price': 3.63745, 'temperable': True, 'annealed': False},
        {'name': 'LoE i89 4mm', 'thickness': '4mm', 'base_price': 3.35913, 'temperable': True, 'annealed': False},
        {'name': 'LoE i89 5mm', 'thickness': '5mm', 'base_price': 3.54463, 'temperable': True, 'annealed': False},
        {'name': 'LoE i89 6mm', 'thickness': '6mm', 'base_price': 3.85121, 'temperable': True, 'annealed': False},
        {'name': 'Planitherm 4mm', 'thickness': '4mm', 'base_price': 2.7508, 'temperable': False, 'annealed': True},
        {'name': 'Planitherm II EZP 4mm', 'thickness': '4mm', 'base_price': 2.94515, 'temperable': True, 'annealed': True},
        {'name': 'Planitherm Lux 4mm', 'thickness': '4mm', 'base_price': 2.613, 'temperable': False, 'annealed': True},
        {'name': 'Planitherm Lux II EZP 4mm', 'thickness': '4mm', 'base_price': 2.691, 'temperable': True, 'annealed': True},
        {'name': 'Planitherm 6mm', 'thickness': '6mm', 'base_price': 3.81225, 'temperable': False, 'annealed': True},
        {'name': 'Planitherm II EZP 6mm', 'thickness': '6mm', 'base_price': 3.81225, 'temperable': True, 'annealed': True},
        {'name': 'Planitherm Lux 6mm', 'thickness': '6mm', 'base_price': 3.51, 'temperable': False, 'annealed': True},
        {'name': 'Planitherm Lux II EZP 6mm', 'thickness': '6mm', 'base_price': 3.588, 'temperable': True, 'annealed': True},
        {'name': 'SKN176 4mm', 'thickness': '4mm', 'base_price': 3.3488, 'temperable': False, 'annealed': False},
        {'name': 'SKN176II EZP 4mm', 'thickness': '4mm', 'base_price': 3.48335, 'temperable': True, 'annealed': False},
        {'name': 'SKN176 6mm', 'thickness': '6mm', 'base_price': 4.49995, 'temperable': False, 'annealed': False},
        {'name': 'SKN176II EZP 6mm', 'thickness': '6mm', 'base_price': 4.6345, 'temperable': True, 'annealed': False},
        {'name': 'Superspacer 10mm / sqf', 'thickness': '10mm', 'base_price': 0.727776, 'temperable': False, 'annealed': False},
        {'name': 'Superspacer 11mm / sqf', 'thickness': '11mm', 'base_price': 0.795207, 'temperable': False, 'annealed': False},
        {'name': 'Superspacer 12mm / sqf', 'thickness': '12mm', 'base_price': 0.802788, 'temperable': False, 'annealed': False},
        {'name': 'Superspacer 14mm / sqf', 'thickness': '14mm', 'base_price': 0.881657, 'temperable': False, 'annealed': False},
        {'name': 'Superspacer 16mm / sqf', 'thickness': '16mm', 'base_price': 0.958531, 'temperable': False, 'annealed': False},
        {'name': 'Superspacer 18mm / sqf', 'thickness': '18mm', 'base_price': 0.967974, 'temperable': False, 'annealed': False},
        {'name': 'Superspacer 20mm / sqf', 'thickness': '20mm', 'base_price': 1.123584, 'temperable': False, 'annealed': False},
        {'name': 'Superspacer 24mm / sqf', 'thickness': '24mm', 'base_price': 1.443582, 'temperable': False, 'annealed': False},
        {'name': 'Technoform 11mm / sqf', 'thickness': '11mm', 'base_price': 0.70, 'temperable': False, 'annealed': False},
        {'name': 'Technoform 12mm / sqf', 'thickness': '12mm', 'base_price': 0.74, 'temperable': False, 'annealed': False},
        {'name': 'Technoform 14mm / sqf', 'thickness': '14mm', 'base_price': 0.82, 'temperable': False, 'annealed': False},
        {'name': 'Technoform 16mm / sqf', 'thickness': '16mm', 'base_price': 0.90, 'temperable': False, 'annealed': False},
        {'name': 'Technoform 18mm / sqf', 'thickness': '18mm', 'base_price': 0.96, 'temperable': False, 'annealed': False},
        {'name': 'Technoform 20mm / sqf', 'thickness': '20mm', 'base_price': 1.04, 'temperable': False, 'annealed': False},
        {'name': 'Technoform 25mm / sqf', 'thickness': '25mm', 'base_price': 1.20, 'temperable': False, 'annealed': False},
        {'name': 'Technoform 27mm / sqf', 'thickness': '27mm', 'base_price': 1.28, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  10mm Polyurethane', 'thickness': '10mm', 'base_price': 0.1483, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  10mm Silicone', 'thickness': '10mm', 'base_price': 0.1796, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  11mm Polyurethane', 'thickness': '11mm', 'base_price': 0.1624, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  11mm Silicone', 'thickness': '11mm', 'base_price': 0.1967, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  12mm Polyurethane', 'thickness': '12mm', 'base_price': 0.1695, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  12mm Silicone', 'thickness': '12mm', 'base_price': 0.2052, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  14mm Polyurethane', 'thickness': '14mm', 'base_price': 0.1977, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  14mm Silicone', 'thickness': '14mm', 'base_price': 0.2394, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  16mm Polyurethane', 'thickness': '16mm', 'base_price': 0.2260, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  16mm Silicone', 'thickness': '16mm', 'base_price': 0.2736, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  18mm Polyurethane', 'thickness': '18mm', 'base_price': 0.2542, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  18mm Silicone', 'thickness': '18mm', 'base_price': 0.3078, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  20mm Polyurethane', 'thickness': '20mm', 'base_price': 0.2825, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  20mm Silicone', 'thickness': '20mm', 'base_price': 0.3421, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  24mm Polyurethane', 'thickness': '24mm', 'base_price': 0.3390, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  24mm Silicone', 'thickness': '24mm', 'base_price': 0.4105, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  26mm Polyurethane', 'thickness': '26mm', 'base_price': 0.3672, 'temperable': False, 'annealed': False},
        {'name': 'Sealant -  26mm Silicone', 'thickness': '26mm', 'base_price': 0.4447, 'temperable': False, 'annealed': False},
    ]
GLASS = [p for p in PRICING if 'mm' in p['name'].lower() and not any(x in p['name'].lower() for x in ['temper', 'spacer', 'sealant', 'technoform'])]
SPACERS = [p for p in PRICING if 'spacer' in p['name'].lower() or 'technoform' in p['name'].lower()]
SEALANTS = [p for p in PRICING if 'sealant' in p['name'].lower()]

TIERS = {
    'T1': {'glass': 30, 'spacer': 20, 'sealant': 20, 'temper': 0, 'name': 'Internal'},
    'T2': {'glass': 40, 'spacer': 40, 'sealant': 40, 'temper': 0, 'name': 'ORA Canada'},
    'T3': {'glass': 60, 'spacer': 60, 'sealant': 60, 'temper': 0, 'name': 'Tier 3'},
    'T4': {'glass': 75, 'spacer': 75, 'sealant': 75, 'temper': 0, 'name': 'Tier 4'},
}

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').lower()
        password = request.form.get('password', '')
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        user = USERS.get(username)
        if user and user['password'] == password_hash:
            session['user'] = username
            session['role'] = user['role']
            session['name'] = user['name']
            return redirect(url_for('dashboard'))
        flash('Invalid username or password', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    user = USERS.get(session['user'], {})
    role = ROLES.get(user.get('role'), {})
    return render_template('dashboard.html', 
                          user=session, 
                          modules=role.get('modules', []),
                          role_name=role.get('name', ''))

@app.route('/quotes')
@login_required
@module_required('quotes')
def quotes():
    return render_template('quotes.html', 
                          glass=GLASS, 
                          spacers=SPACERS, 
                          sealants=SEALANTS, 
                          tiers=TIERS,
                          user=session)

@app.route('/api/calculate', methods=['POST'])
@login_required
def calculate():
    data = request.json
    tier = data.get('tier', 'T2')
    pct = TIERS.get(tier, TIERS['T2'])
    def apply_tier(base, item_type):
        return base * (1 + pct.get(item_type, 0) / 100)
    total = 0
    breakdown = []
    layers = [
        (data.get('glass1'), data.get('temper1')),
        (data.get('glass2'), data.get('temper2')),
        (data.get('glass3'), data.get('temper3'))
    ]
    for glass_name, temper in layers:
        if glass_name and glass_name != 'n/a':
            glass = next((g for g in PRICING if g['name'] == glass_name), None)
            if glass:
                p = apply_tier(glass['base_price'], 'glass')
                total += p
                breakdown.append({'item': glass['name'], 'price': round(p, 2)})
                if temper == 'yes' and glass.get('temperable'):
                    total += 0.65
                    breakdown.append({'item': 'Tempering', 'price': 0.65})
    for spacer_name in [data.get('spacer1'), data.get('spacer2')]:
        if spacer_name and spacer_name != 'n/a':
            spacer = next((s for s in PRICING if s['name'] == spacer_name), None)
            if spacer:
                p = apply_tier(spacer['base_price'], 'spacer')
                total += p
                breakdown.append({'item': spacer['name'], 'price': round(p, 2)})
    for sealant_name in [data.get('sealant1'), data.get('sealant2')]:
        if sealant_name and sealant_name != 'n/a':
            sealant = next((s for s in PRICING if s['name'] == sealant_name), None)
            if sealant:
                p = apply_tier(sealant['base_price'], 'sealant')
                total += p
                breakdown.append({'item': sealant['name'], 'price': round(p, 2)})
    return jsonify({'price_sqf': round(total, 2), 'breakdown': breakdown, 'tier': tier})

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
