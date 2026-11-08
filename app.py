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
    'admin': {'name': 'Administrator', 'modules': ['quotes', 'orders', 'customers', 'inventory', 'pricing', 'users']},
    'sales': {'name': 'Sales', 'modules': ['quotes', 'orders', 'customers']},
    'production': {'name': 'Production', 'modules': ['orders', 'inventory']},
    'viewer': {'name': 'Viewer', 'modules': ['quotes', 'orders']},
    'customer': {'name': 'Customer', 'modules': ['quotes', 'orders']}
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

PRICING = [
    {'name': 'Clear Glass 4mm', 'sheet_size': '130"x84"', 'base_price': 1.1232, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 5mm', 'sheet_size': '144"x84"', 'base_price': 1.4391, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 6mm', 'sheet_size': '144"x96"', 'base_price': 1.66725, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 6mm Jumbo', 'sheet_size': '204"x130"', 'base_price': 2.38875, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 8mm', 'sheet_size': '144"x96"', 'base_price': 2.3814, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 8mm Jumbo', 'sheet_size': '204"x130"', 'base_price': 3.7044, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 10mm', 'sheet_size': '144"x96"', 'base_price': 3.0051, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 10mm Jumbo', 'sheet_size': '204"x130"', 'base_price': 4.3953, 'temperable': True, 'annealed': True},
