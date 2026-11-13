// Both QuickBooks cards must read their own fields. closest() includes the
// button itself, and the buttons used to carry data-connection, so the click
// handler treated the button as the card and then read .value of null.
const fs = require('fs');
const html = fs.readFileSync('templates/quickbooks.html', 'utf8');
const source = html.match(/function cardOf\(node\) \{[\s\S]*?\n\}/)[0]
  + '\n'
  + html.match(/function fieldValue\(card, name\) \{[\s\S]*?\n\}/)[0];
eval(source);

function matches(el, sel) {
  if (sel === '.card[data-connection]') {
    return el.attrs.class === 'card' && el.attrs['data-connection'];
  }
  const attr = sel.match(/^\[data-field="([^"]+)"\]$/);
  if (attr) return el.attrs['data-field'] === attr[1];
  const action = sel.match(/^\[data-action="([^"]+)"\]$/);
  if (action) return el.attrs['data-action'] === action[1];
  throw new Error('selector not supported: ' + sel);
}

function walk(el, out) {
  out.push(el);
  for (const child of el.children) walk(child, out);
}

class El {
  constructor(tag, attrs) {
    this.tag = tag;
    this.attrs = attrs || {};
    this.children = [];
    this.parentElement = null;
    this.value = '';
  }
  closest(sel) {
    let node = this;
    while (node) {
      if (matches(node, sel)) return node;
      node = node.parentElement;
    }
    return null;
  }
  querySelector(sel) {
    const all = [];
    walk(this, all);
    return all.find(el => matches(el, sel)) || null;
  }
  append(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
}

function card(connection, qboId, customerId) {
  const root = new El('div', {class: 'card', 'data-connection': connection});
  const row = root.append(new El('div', {class: 'row'}));
  const qbo = row.append(new El('input', {'data-field': 'link-qbo'}));
  qbo.value = qboId;
  const erp = row.append(new El('select', {'data-field': 'link-erp'}));
  erp.value = customerId;
  const link = row.append(new El('button', {'data-action': 'link-customer'}));
  const unlink = row.append(new El('button', {'data-action': 'unlink-customer'}));
  const importing = root.append(new El('button', {'data-action': 'import-history'}));
  return {root, link, unlink, importing};
}

function posted(button) {
  const found = cardOf(button);
  const connection = found && found.attrs['data-connection'];
  return {
    connection,
    qbo_id: fieldValue(found, 'link-qbo'),
    customer_id: fieldValue(found, 'link-erp'),
  };
}

const us = card('us', '2', 'open-windows');
const ca = card('ca', '88', 'maple');
const usPost = posted(us.link);
const caPost = posted(ca.link);
const usImport = cardOf(us.importing).attrs['data-connection'];
const caImport = cardOf(ca.importing).attrs['data-connection'];
if (usPost.connection !== 'us' || usPost.qbo_id !== '2' || usPost.customer_id !== 'open-windows') {
  throw new Error('US link read the wrong card: ' + JSON.stringify(usPost));
}
if (caPost.connection !== 'ca' || caPost.qbo_id !== '88' || caPost.customer_id !== 'maple') {
  throw new Error('Canada link read the wrong card: ' + JSON.stringify(caPost));
}
if (usImport !== 'us' || caImport !== 'ca') {
  throw new Error('Import buttons resolved ' + usImport + ' and ' + caImport);
}
if (cardOf(us.unlink).attrs['data-connection'] !== 'us') {
  throw new Error('US unlink resolved the wrong card');
}
if (cardOf(ca.unlink).attrs['data-connection'] !== 'ca') {
  throw new Error('Canada unlink resolved the wrong card');
}
console.log('both cards read their own link and import fields');
