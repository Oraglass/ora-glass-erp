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
GLASS = [p for p in PRICING if 'mm' in p['name'].lower() and not any(x in p['name'].lower() for x in ['temper', 'spacer', 'sealant'])]
SPACERS = [p for p in PRICING if 'spacer' in p['name'].lower()]
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
    glass1 = next((g for g in PRICING if g['name'] == data.get('glass1')), None)
    glass2 = next((g for g in PRICING if g['name'] == data.get('glass2')), None)
    spacer = next((s for s in PRICING if s['name'] == data.get('spacer')), None)
    sealant = next((s for s in PRICING if s['name'] == data.get('sealant')), None)
    tier = data.get('tier', 'T2')
    tempered = data.get('tempered', False)
    
    pct = TIERS.get(tier, TIERS['T2'])
    
    def apply_tier(base, item_type):
        return base * (1 + pct.get(item_type, 0) / 100)
    
    total = 0
    breakdown = []
    
    if glass1:
        p = apply_tier(glass1['base_price'], 'glass')
        total += p
        breakdown.append({'item': glass1['name'], 'price': round(p, 2)})
        if tempered and glass1['temperable']:
            total += 0.65
            breakdown.append({'item': 'Tempering', 'price': 0.65})
    
    if spacer:
        p = apply_tier(spacer['base_price'], 'spacer')
        total += p
        breakdown.append({'item': spacer['name'], 'price': round(p, 2)})
    
    if sealant:
        p = apply_tier(sealant['base_price'], 'sealant')
        total += p
        breakdown.append({'item': sealant['name'], 'price': round(p, 2)})
    
    if glass2:
        p = apply_tier(glass2['base_price'], 'glass')
        total += p
        breakdown.append({'item': glass2['name'], 'price': round(p, 2)})
        if tempered and glass2['temperable']:
            total += 0.65
            breakdown.append({'item': 'Tempering', 'price': 0.65})
    
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
