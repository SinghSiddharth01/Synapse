"use strict";

// A hand-rolled DOM, just big enough to actually execute the /debug page's
// real <script> under Node (test_debug_page_js.py extracts it verbatim from
// the served page) and assert on client-side behavior -- not merely on
// whether some string appears in the HTML source. Supports exactly what
// that page's JS uses: getElementById/createElement/querySelectorAll on a
// fake `document`, classList/attributes/innerHTML/textContent/appendChild/
// nextElementSibling/closest/addEventListener on elements, and CSS
// selectors of the shape #id, .class, [attr] / [attr="v"] / [attr='v'],
// space-separated for one level of descendant ("#feed .entry"). Nothing
// more general than that is implemented, and nothing here needs to be:
// this is scoped to making the shipped page provably testable, not a
// general-purpose DOM.

class FakeElement {
  constructor(tag, attrs) {
    this.tag = tag;
    this.attrs = Object.assign({}, attrs || {});
    this.children = [];
    this.parent = null;
    this._listeners = {};
    this.style = {};
    this._text = "";
    this._escaped = "";
  }
  get classList() {
    var self = this;
    function names() { return (self.attrs["class"] || "").split(/\s+/).filter(Boolean); }
    function set(list) { self.attrs["class"] = list.join(" "); }
    return {
      contains: function (c) { return names().indexOf(c) !== -1; },
      add: function (c) { var n = names(); if (n.indexOf(c) === -1) { n.push(c); set(n); } },
      remove: function (c) { set(names().filter(function (x) { return x !== c; })); },
      toggle: function (c) {
        var n = names();
        var idx = n.indexOf(c);
        if (idx !== -1) { n.splice(idx, 1); set(n); return false; }
        n.push(c); set(n); return true;
      },
    };
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  appendChild(child) {
    if (child.parent) {
      var i = child.parent.children.indexOf(child);
      if (i !== -1) child.parent.children.splice(i, 1);
    }
    child.parent = this;
    this.children.push(child);
    return child;
  }
  get nextElementSibling() {
    if (!this.parent) return null;
    var i = this.parent.children.indexOf(this);
    return this.parent.children[i + 1] || null;
  }
  set innerHTML(html) {
    this.children = parseFragment(html);
    for (var i = 0; i < this.children.length; i++) this.children[i].parent = this;
    // Retain the raw markup as well as the parsed children. parseFragment
    // only understands <div>, so a renderer that emits a <table> (the brain
    // page's roster) would otherwise be completely invisible to a driver --
    // and it is the one element on that page most worth asserting on. Reading
    // this back is the last-written HTML either way, which is what a real
    // getter does; `esc()` still round-trips through the textContent setter
    // below and is unaffected.
    this._escaped = String(html);
  }
  get innerHTML() { return this._escaped; }
  set textContent(text) {
    this._text = String(text === null || text === undefined ? "" : text);
    this.children = [];
    this._escaped = this._text.replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  get textContent() { return this._text; }
  addEventListener(type, handler) {
    (this._listeners[type] = this._listeners[type] || []).push(handler);
  }
  dispatchClick(target) {
    var handlers = this._listeners["click"] || [];
    for (var i = 0; i < handlers.length; i++) handlers[i]({ target: target });
  }
  dispatchChange(value) {
    this.value = value;
    var handlers = this._listeners["change"] || [];
    for (var i = 0; i < handlers.length; i++) handlers[i]({ target: this });
  }
  matches(selector) { return matchesSimple(this, selector); }
  closest(selector) {
    var node = this;
    while (node) {
      if (node.matches && node.matches(selector)) return node;
      node = node.parent;
    }
    return null;
  }
  querySelectorAll(selector) {
    var parts = selector.trim().split(/\s+/);
    if (parts.length === 1) {
      var results = [];
      (function walk(node) {
        for (var i = 0; i < node.children.length; i++) {
          var c = node.children[i];
          if (c.matches(parts[0])) results.push(c);
          walk(c);
        }
      })(this);
      return results;
    }
    var first = parts[0];
    var rest = parts.slice(1).join(" ");
    var scope = first[0] === "#" ? REGISTRY[first.slice(1)] : this;
    return scope ? scope.querySelectorAll(rest) : [];
  }
}

function parseAttrs(attrStr) {
  var attrs = {};
  var re = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"/g;
  var m;
  while ((m = re.exec(attrStr))) attrs[m[1]] = m[2];
  return attrs;
}

function parseFragment(html) {
  var out = [];
  var re = /<div([^>]*)>([\s\S]*?)<\/div>/g;
  var m;
  while ((m = re.exec(html))) {
    out.push(new FakeElement("div", parseAttrs(m[1])));
  }
  return out;
}

function matchesSimple(el, selector) {
  // Supports #id, .class (repeatable), [attr] / [attr="v"] / [attr='v'] (repeatable), any order.
  var re = /#([-\w]+)|\.([-\w]+)|\[([-\w]+)(?:=(["'])(.*?)\4)?\]/g;
  var m;
  var ok = true;
  while ((m = re.exec(selector))) {
    if (m[1] !== undefined) {
      if (el.attrs.id !== m[1]) ok = false;
    } else if (m[2] !== undefined) {
      if (!el.classList.contains(m[2])) ok = false;
    } else if (m[3] !== undefined) {
      if (!Object.prototype.hasOwnProperty.call(el.attrs, m[3])) ok = false;
      else if (m[5] !== undefined && el.attrs[m[3]] !== m[5]) ok = false;
    }
  }
  return ok;
}

var REGISTRY = {};
function seed(tag, id) {
  var el = new FakeElement(tag, { id: id });
  REGISTRY[id] = el;
  return el;
}

var document = {
  getElementById: function (id) { return REGISTRY[id] || null; },
  createElement: function (tag) { return new FakeElement(tag, {}); },
  querySelectorAll: function (selector) {
    var parts = selector.trim().split(/\s+/);
    var first = parts[0];
    var rest = parts.slice(1).join(" ");
    if (first[0] !== "#") return [];
    var scope = REGISTRY[first.slice(1)];
    return scope ? scope.querySelectorAll(rest) : [];
  },
};
