import json
import os

from storage import (
    BASE_DIR,
    atomic_write_json,
    customers_path,
    data_dir,
    load_quotes,
    quotes_path,
    resolve_pricing_path,
    save_customers,
    save_quotes,
    save_tiers,
    save_users,
)


def test_quotes_path_creates_parent_and_roundtrips(tmp_path, monkeypatch):
    target = tmp_path / 'nested' / 'disk' / 'quotes.json'
    monkeypatch.setenv('QUOTES_PATH', str(target))
    save_quotes([{'id': 3, 'project': 'Tower'}])
    assert target.exists()
    assert json.loads(target.read_text())[0]['project'] == 'Tower'
    assert load_quotes()[0]['id'] == 3
    assert list(target.parent.glob('.tmp-*')) == []
    assert quotes_path() == str(target)


def test_data_dir_follows_quotes_path_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv('DATA_DIR', raising=False)
    monkeypatch.setenv('QUOTES_PATH', str(tmp_path / 'persist' / 'quotes.json'))
    assert os.path.abspath(data_dir()) == str(tmp_path / 'persist')
    assert os.path.abspath(customers_path()) == str(tmp_path / 'persist' / 'customers.json')


def test_explicit_paths_are_independent(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('QUOTES_PATH', str(tmp_path / 'other' / 'quotes.json'))
    monkeypatch.setenv('CUSTOMERS_PATH', str(tmp_path / 'c' / 'customers.json'))
    monkeypatch.setenv('USERS_PATH', str(tmp_path / 'u' / 'users.json'))
    monkeypatch.setenv('TIERS_PATH', str(tmp_path / 't' / 'tiers.json'))
    save_quotes([{'id': 1}])
    save_customers({'acme': {'name': 'Acme'}})
    save_users({'a@b.co': {'email': 'a@b.co'}})
    save_tiers({'T2': {'label': 'Tier 2', 'glass_markup': 0.4, 'spacer_markup': 0.4, 'placeholder': False}})
    assert json.loads((tmp_path / 'other' / 'quotes.json').read_text())[0]['id'] == 1
    assert json.loads((tmp_path / 'c' / 'customers.json').read_text())['acme']['name'] == 'Acme'
    assert 'a@b.co' in json.loads((tmp_path / 'u' / 'users.json').read_text())


def test_legacy_quotes_file_migrates_to_quotes_path(tmp_path, monkeypatch):
    legacy_root = tmp_path / 'legacy'
    legacy_root.mkdir()
    (legacy_root / 'quotes.json').write_text(json.dumps([{'id': 7, 'project': 'Old'}]), encoding='utf-8')
    dest = tmp_path / 'disk' / 'quotes.json'
    monkeypatch.setenv('QUOTES_PATH', str(dest))
    loaded = load_quotes(legacy_dir=str(legacy_root))
    assert loaded[0]['project'] == 'Old'
    assert json.loads(dest.read_text())[0]['id'] == 7
    assert (legacy_root / 'quotes.json').exists()


def test_atomic_write_replaces_existing_file(tmp_path):
    target = tmp_path / 'quotes.json'
    target.write_text('not json', encoding='utf-8')
    atomic_write_json(str(target), [{'ok': True}])
    assert json.loads(target.read_text())[0]['ok'] is True
    assert list(tmp_path.glob('.tmp-*')) == []


def test_pricing_path_prefers_repo_root_over_data_copy(tmp_path, monkeypatch):
    root = tmp_path / 'app'
    data = root / 'data'
    data.mkdir(parents=True)
    (root / 'pricing_base.xlsx').write_bytes(b'root')
    (data / 'pricing_base.xlsx').write_bytes(b'data')
    monkeypatch.delenv('PRICING_PATH', raising=False)
    chosen = resolve_pricing_path(base_dir=str(root), data_directory=str(data))
    assert chosen == str(root / 'pricing_base.xlsx')


def test_pricing_path_uses_data_copy_when_root_missing(tmp_path, monkeypatch):
    root = tmp_path / 'app'
    data = root / 'data'
    data.mkdir(parents=True)
    (data / 'pricing_base.xlsx').write_bytes(b'data')
    monkeypatch.delenv('PRICING_PATH', raising=False)
    chosen = resolve_pricing_path(base_dir=str(root), data_directory=str(data))
    assert chosen == str(data / 'pricing_base.xlsx')


def test_pricing_path_env_wins(tmp_path, monkeypatch):
    env_file = tmp_path / 'custom.xlsx'
    env_file.write_bytes(b'custom')
    monkeypatch.setenv('PRICING_PATH', str(env_file))
    assert resolve_pricing_path(base_dir=str(tmp_path), data_directory=str(tmp_path / 'data')) == str(env_file)


def test_repo_pricing_file_loads_without_a_data_copy():
    from app import GLASS, PRICING, PRICING_SOURCE, SEALANTS
    workbook = os.path.join(BASE_DIR, 'pricing_base.xlsx')
    assert os.path.isabs(PRICING_SOURCE)
    assert os.path.samefile(PRICING_SOURCE, workbook)
    glass = next(item for item in PRICING if item['name'] == 'Clear Glass 4mm')
    assert abs(glass['base_price'] - 1.1232) < 0.0001
    sealant = next(item for item in SEALANTS if item['name'] == 'Sealant -  14mm Polyurethane')
    assert sealant['base_price'] == 0.19774990969631806
    assert all('tempering' not in item['name'].lower() for item in GLASS)
    assert not os.path.exists(os.path.join(BASE_DIR, 'data', 'pricing_base.xlsx'))


def test_workbook_loads_beside_app_when_cwd_is_elsewhere(tmp_path, monkeypatch):
    monkeypatch.delenv('PRICING_PATH', raising=False)
    monkeypatch.chdir(tmp_path)
    from quoting import load_pricing
    pricing, source = load_pricing()
    workbook = os.path.join(BASE_DIR, 'pricing_base.xlsx')
    assert os.path.isabs(source)
    assert os.path.samefile(source, workbook)
    assert any(item['name'] == 'Sealant -  14mm Polyurethane' for item in pricing)
    assert not os.path.exists(os.path.join(tmp_path, 'data', 'pricing_base.xlsx'))


def test_fallback_sealants_use_workbook_prices_without_new_technoform_prices():
    from quoting import FALLBACK_PRICING, load_pricing_workbook
    workbook = load_pricing_workbook(os.path.join(BASE_DIR, 'pricing_base.xlsx'))

    def normalize(name):
        return ' '.join(name.split())

    book = {
        normalize(item['name']): item['base_price']
        for item in workbook
        if 'sealant' in item['name'].lower()
    }
    fallback = {
        normalize(item['name']): item['base_price']
        for item in FALLBACK_PRICING
        if 'sealant' in item['name'].lower()
    }
    assert set(fallback) == set(book)
    for name, price in book.items():
        assert fallback[name] == price
    technoform = [item for item in FALLBACK_PRICING if 'technoform' in item['name'].lower()]
    assert [item['name'] for item in technoform] == [
        'Technoform 11mm / sqf',
        'Technoform 12mm / sqf',
        'Technoform 14mm / sqf',
        'Technoform 16mm / sqf',
        'Technoform 18mm / sqf',
        'Technoform 20mm / sqf',
    ]
    assert all(item['base_price'] > 0 for item in technoform)
