#!/usr/bin/env python3
"""
SoapDesk - hafif masaustu WSDL/SOAP test istemcisi.

WSDL adresini ac, operation listesini gor, hazir envelope sablonunu al,
duzenle, gonder. SoapUI'nin yaptigi is, Java ve Electron olmadan.

Kurulum:
    pip install zeep
Calistirma:
    python soapdesk.py

Windows'ta python.org kurulumu tkinter'i iceriyor, ek bir sey gerekmez.
Linux'ta gerekirse: apt install python3-tk
"""
import os
import re
import sys
import bisect
import json
import queue
import threading
import datetime as _dt

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

try:
    import requests
    import urllib3
    from lxml import etree
    from zeep import Client, Transport, Settings
    from zeep.wsdl.bindings.soap import Soap12Binding
    from zeep.xsd.types.complex import ComplexType
    from zeep.xsd.elements.element import Element
    from zeep.xsd.elements.any import Any
    from zeep.xsd.elements.indicators import Choice, Group
except ImportError as e:
    sys.exit("Eksik bagimlilik: %s\nCozum: pip install zeep" % e)

urllib3.disable_warnings()

APP = "SoapDesk"
CFG = os.path.join(os.path.expanduser("~"), ".soapdesk.json")
HISTORY = os.path.join(os.path.expanduser("~"), "soapdesk_history.xml")
MAXDEPTH = 8



# --------------------------------------------------------------------------
# WSDL motoru
# --------------------------------------------------------------------------
def load_wsdl(url, verify=True, timeout=60):
    s = requests.Session()
    s.verify = verify
    tr = Transport(session=s, timeout=timeout, operation_timeout=timeout)
    # strict=False: mukerrer global element gibi bozuk WSDL'leri tolere eder
    return Client(url, transport=tr,
                  settings=Settings(strict=False, xml_huge_tree=True))


def list_operations(client):
    out = []
    for sname, service in client.wsdl.services.items():
        for pname, port in service.ports.items():
            try:
                names = sorted(port.binding.all().keys())
            except Exception:
                names = []
            out.append((sname, pname, names))
    return out


ENV11 = 'http://schemas.xmlsoap.org/soap/envelope/'
ENV12 = 'http://www.w3.org/2003/05/soap-envelope'
RESERVED = {'xs', 'xsd', 'xsi', 'soap', 'soap11', 'soap12', 'soapenv',
            'wsdl', 'wsdlsoap', 'http', 'mime', 'tm'}


def _qn(q):
    """zeep qname (QName veya str) -> lxml'in kabul ettigi form."""
    return str(q)


def _ns_of(q):
    try:
        return etree.QName(str(q)).namespace
    except Exception:
        return None


def _prefix_for(client, ns):
    """Bu namespace icin okunabilir bir prefix uret (v3, tns vb)."""
    try:
        for p, u in client.wsdl.types.prefix_map.items():
            if u == ns and p and p not in RESERVED \
                    and not re.match(r'^ns\d+$', p):
                return p
    except Exception:
        pass
    # zeep prefix'leri ns0'a normalize ediyor; namespace'in son parcasindan uret
    tail = [x for x in (ns or '').replace('#', '/').split('/') if x]
    if tail and re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,11}$', tail[-1]):
        return tail[-1]
    return 'ns0'


def _c(parent, txt):
    parent.append(etree.Comment(' %s ' % txt))


def _render(parent, particle, depth, seen):
    """Bir indicator (Sequence/Choice/All/Group) veya Element'i XML'e bas."""
    if isinstance(particle, Element):
        _render_element(parent, particle, depth, seen)
        return
    if isinstance(particle, Any):
        _c(parent, 'You may enter ANY elements at this point')
        return
    if isinstance(particle, Choice):
        kids = list(particle)
        if kids:
            _c(parent, 'You have a CHOICE of the next %d items at this level'
               % len(kids))
        for ch in kids:
            _render(parent, ch, depth, seen)
        return
    if isinstance(particle, Group):
        child = getattr(particle, 'child', None)
        if child is not None:
            _render(parent, child, depth, seen)
            return
    # Sequence / All / bilinmeyen container
    try:
        for ch in particle:
            _render(parent, ch, depth, seen)
    except TypeError:
        pass


def _render_element(parent, elm, depth, seen):
    if elm.max_occurs != 1:
        _c(parent, 'Zero or more repetitions:' if elm.min_occurs == 0
           else '1 or more repetitions:')
    elif elm.min_occurs == 0:
        _c(parent, 'Optional:')

    node = etree.SubElement(parent, _qn(elm.qname))
    t = elm.type

    if not isinstance(t, ComplexType):
        node.text = '?'
        return

    for _name, attr in (t.attributes or []):
        try:
            node.set(_qn(getattr(attr, 'qname', None) or _name), '?')
        except Exception:
            pass

    inner = getattr(t, '_element', None)
    if inner is None:
        node.text = '?'          # simpleContent + attribute
        return

    key = id(t)
    if depth >= MAXDEPTH or key in seen:
        _c(node, 'recursive type - elle doldurun')
        return
    _render(node, inner, depth + 1, seen | {key})


def build_template(client, sname, pname, oname):
    port = client.wsdl.services[sname].ports[pname]
    binding = port.binding
    envns = ENV12 if isinstance(binding, Soap12Binding) else ENV11
    op = binding.get(oname)
    body_elm = getattr(op.input, 'body', None)

    if body_elm is None:                     # rpc/encoded vb: zeep'e devret
        return _template_fallback(client, sname, pname, oname)

    nsmap = {'soapenv': envns}
    tns = _ns_of(getattr(body_elm, 'qname', None))
    if tns:
        nsmap[_prefix_for(client, tns)] = tns

    env = etree.Element(etree.QName(envns, 'Envelope'), nsmap=nsmap)
    hdr = etree.SubElement(env, etree.QName(envns, 'Header'))
    body = etree.SubElement(env, etree.QName(envns, 'Body'))

    try:                                     # SOAP header tanimliysa bas
        h = getattr(op.input, 'header', None)
        hin = getattr(h, '_element', None) if h is not None else None
        if hin is not None:
            _render(hdr, hin, 0, frozenset())
    except Exception:
        pass

    _render_element(body, body_elm, 0, frozenset())
    # SoapUI gibi <?xml?> bildirimi koymuyoruz: kullanici basa bir sey
    # eklediginde belge bozulmasin
    return etree.tostring(env, pretty_print=True).decode()


def _template_fallback(client, sname, pname, oname):
    """Dokuman stili olmayan binding'ler icin zeep uzerinden uret."""
    svc = client.bind(sname, pname)
    node = client.create_message(svc, oname)
    return etree.tostring(node, pretty_print=True).decode()


def build_endpoint(client, sname, pname, oname):
    port = client.wsdl.services[sname].ports[pname]
    b = port.binding
    addr = port.binding_options.get('address', '')
    action = getattr(b.get(oname), 'soapaction', '') or ''
    if isinstance(b, Soap12Binding):
        ct = 'application/soap+xml; charset=utf-8'
        if action:
            ct += '; action="%s"' % action
        hdr = {'Content-Type': ct}
    else:
        hdr = {'Content-Type': 'text/xml; charset=utf-8',
               'SOAPAction': '"%s"' % action}
    return addr, hdr


def post_envelope(addr, headers, xml, verify=True, timeout=180):
    r = requests.post(addr, data=xml.encode('utf-8'), headers=headers,
                      verify=verify, timeout=timeout)
    try:
        body = etree.tostring(etree.fromstring(r.content), pretty_print=True,
                              encoding='utf-8').decode()
    except Exception:
        body = r.text
    return r.status_code, dict(r.headers), body


# --------------------------------------------------------------------------
# Gecmis deposu (XML)
# --------------------------------------------------------------------------
_BADCHR = re.compile(u'[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD]')


def _clean(s):
    return _BADCHR.sub('', s or '')


def _cdata(el, text):
    t = _clean(text).replace(']]>', ']]&gt;')
    try:
        el.text = etree.CDATA(t)
    except Exception:
        el.text = t


class HistoryStore(object):
    """Gonderilen request'leri ve taslaklari tek bir XML dosyasinda tutar."""

    def __init__(self, path):
        self.path = path
        self.entries = []
        self.seq = 0

    # ---- disk
    def load(self):
        self.entries = []
        if not os.path.exists(self.path):
            return
        try:
            root = etree.parse(self.path).getroot()
        except Exception:
            return
        for e in root.findall('entry'):
            hdrs = {}
            for h in e.findall('headers/header'):
                hdrs[h.get('name', '')] = h.text or ''
            self.entries.append({
                'id': int(e.get('id') or 0),
                'kind': e.get('kind', 'sent'),
                'ts': e.get('ts', ''),
                'wsdl': e.get('wsdl', ''),
                'service': e.get('service', ''),
                'port': e.get('port', ''),
                'operation': e.get('operation', ''),
                'endpoint': e.get('endpoint', ''),
                'status': e.get('status', ''),
                'ms': e.get('ms', ''),
                'headers': hdrs,
                'envelope': (e.findtext('envelope') or ''),
                'response': (e.findtext('response') or ''),
            })
        self.seq = max([x['id'] for x in self.entries] or [0])

    def flush(self):
        root = etree.Element('soapdesk', version='1')
        for x in self.entries:
            e = etree.SubElement(root, 'entry')
            for k in ('id', 'kind', 'ts', 'wsdl', 'service', 'port',
                      'operation', 'endpoint', 'status', 'ms'):
                if x.get(k) not in (None, ''):
                    e.set(k, str(x[k]))
            if x.get('headers'):
                hs = etree.SubElement(e, 'headers')
                for k, v in x['headers'].items():
                    h = etree.SubElement(hs, 'header', name=_clean(k))
                    h.text = _clean(v)
            _cdata(etree.SubElement(e, 'envelope'), x.get('envelope', ''))
            if x.get('kind') == 'sent':
                _cdata(etree.SubElement(e, 'response'), x.get('response', ''))
        tmp = self.path + '.tmp'
        etree.ElementTree(root).write(tmp, pretty_print=True,
                                     xml_declaration=True, encoding='utf-8')
        os.replace(tmp, self.path)

    # ---- kayit
    def add_sent(self, rec, cap=1000):
        self.seq += 1
        rec['id'] = self.seq
        rec['kind'] = 'sent'
        rec['ts'] = _dt.datetime.now().isoformat(timespec='seconds')
        self.entries.append(rec)
        sent = [x for x in self.entries if x['kind'] == 'sent']
        if len(sent) > cap:                       # en eskileri buda
            drop = {id(x) for x in sent[:len(sent) - cap]}
            self.entries = [x for x in self.entries if id(x) not in drop]
        self.flush()
        return rec

    def put_drafts(self, wsdl, drafts):
        """Bu WSDL'e ait taslaklari guncelle (eskilerini degistir)."""
        self.entries = [x for x in self.entries
                        if not (x['kind'] == 'draft' and x['wsdl'] == wsdl)]
        for (sn, pn, on), d in drafts.items():
            self.seq += 1
            self.entries.append({
                'id': self.seq, 'kind': 'draft',
                'ts': _dt.datetime.now().isoformat(timespec='seconds'),
                'wsdl': wsdl, 'service': sn, 'port': pn, 'operation': on,
                'endpoint': d.get('addr', ''), 'status': '', 'ms': '',
                'headers': d.get('headers', {}),
                'envelope': d.get('body', ''), 'response': '',
            })
        self.flush()

    def drafts_for(self, wsdl):
        out = {}
        for x in self.entries:
            if x['kind'] == 'draft' and x['wsdl'] == wsdl:
                out[(x['service'], x['port'], x['operation'])] = {
                    'body': x['envelope'], 'addr': x['endpoint'],
                    'headers': x['headers']}
        return out


# --------------------------------------------------------------------------
# Arayuz
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# XML editoru: renklendirme, satir numarasi, arama, bicimlendirme
# --------------------------------------------------------------------------
PAL = {
    'bg':        '#ffffff',   'fg':      '#1f2328',
    'caret':     '#0969da',   'sel':     '#cfe3fb',
    'curline':   '#f4f8fd',   'gutter':  '#f3f5f7',
    'gutter_fg': '#a8b0bb',   'gutter_on': '#0969da',
    'punct':     '#57606a',   'tag':     '#116329',
    'prefix':    '#8250df',   'attr':    '#0550ae',
    'attrprefix': '#8250df',  'value':   '#0a3069',
    'comment':   '#6e7781',   'cdata':   '#953800',
    'pi':        '#8250df',   'doctype': '#8250df',
    'entity':    '#cf222e',   'pair':    '#daf2e3',
    'find':      '#fff2b2',   'findcur': '#ffcf33',
    'errline':   '#ffebe9',   'ok':      '#1a7f37',
    'err':       '#cf222e',
}
INDENT = '  '
MAX_HL = 400000                      # bu boyutun ustunde renklendirme kapanir

_NM = r'[A-Za-z_][-A-Za-z0-9_.]*'
_TOK = re.compile(
    r'(?P<comment><!--.*?-->)'
    r'|(?P<cdata><!\[CDATA\[.*?\]\]>)'
    r'|(?P<pi><\?.*?\?>)'
    r'|(?P<doctype><![A-Za-z].*?>)'
    r'|(?P<tag></?' + _NM + r'(?::' + _NM + r')?'
    r'(?:"[^"]*"|\'[^\']*\'|[^<>"\'])*>)'
    r'|(?P<entity>&(?:\#[0-9]+|\#x[0-9A-Fa-f]+|' + _NM + r');)',
    re.S)
_HEAD = re.compile(r'^(</?)(?:(' + _NM + r'):)?(' + _NM + r')')
_ATTR = re.compile(r'(?:(' + _NM + r'):)?(' + _NM + r')(\s*=\s*)'
                   r'("[^"]*"|\'[^\']*\')')


def format_xml(src):
    """Tek satirlik XML'i okunur hale getir. Bozuksa exception firlatir."""
    head = ''
    body = src.strip()
    m = re.match(r'<\?xml[^>]*\?>\s*', body)
    if m:
        head = m.group().strip() + '\n'
        body = body[m.end():]
    p = etree.XMLParser(remove_blank_text=True, resolve_entities=False,
                        strip_cdata=False, huge_tree=True)
    root = etree.fromstring(body.encode('utf-8'), p)
    out = etree.tostring(root, pretty_print=True, encoding='unicode')
    return head + out.rstrip('\n')


def tag_stack(src):
    """src icinde acik kalmis etiket adlarini sirayla dondur."""
    st = []
    for m in _TOK.finditer(src):
        if m.lastgroup != 'tag':
            continue
        raw = m.group()
        h = _HEAD.match(raw)
        if not h:
            continue
        nm = (h.group(2) + ':' if h.group(2) else '') + h.group(3)
        if raw.startswith('</'):
            if nm in st:
                while st and st.pop() != nm:
                    pass
        elif not raw.endswith('/>'):
            st.append(nm)
    return st


class _ProxyText(tk.Text):
    """Icerik/imlec degisimini olay olarak yayan Text."""

    def __init__(self, *a, **kw):
        tk.Text.__init__(self, *a, **kw)
        self._orig = self._w + '_o'
        self.tk.call('rename', self._w, self._orig)
        self.tk.createcommand(self._w, self._proxy)

    def _proxy(self, *args):
        try:
            res = self.tk.call((self._orig,) + args)
        except tk.TclError as ex:
            if 'tagged with' in str(ex) or 'bad text index' in str(ex):
                return ''
            raise
        op = args[0] if args else ''
        if op in ('insert', 'delete', 'replace') or \
                (op == 'edit' and args[1:2] and args[1] in ('undo', 'redo')):
            self.event_generate('<<Edited>>', when='tail')
        elif op in ('xview', 'yview') or args[:3] == ('mark', 'set', 'insert'):
            self.event_generate('<<Moved>>', when='tail')
        return res


class XmlEditor(ttk.Frame):
    """Satir numarali, sozdizimi renklendirmeli kucuk XML editoru."""

    TAGS = ('comment', 'cdata', 'pi', 'doctype', 'tag', 'prefix',
            'attr', 'attrprefix', 'value', 'punct', 'entity')

    def __init__(self, master, editable=True, validate=False, size=10):
        ttk.Frame.__init__(self, master)
        self.editable = editable
        self.validate = validate
        self.size = size
        self._job = None
        self._spans = []          # (bas, son, tur, ad) - etiket token'lari
        self._starts = [0]
        self._hits = []
        self._hit = -1

        self.font = tkfont.Font(family='Consolas', size=size)
        self.ital = tkfont.Font(family='Consolas', size=size, slant='italic')

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self.gutter = tk.Canvas(self, width=42, bd=0, highlightthickness=0,
                                bg=PAL['gutter'], takefocus=0)
        self.gutter.grid(row=0, column=0, sticky='ns')

        t = self.text = _ProxyText(
            self, wrap='none', undo=editable, maxundo=-1, autoseparators=True,
            font=self.font, bg=PAL['bg'], fg=PAL['fg'], bd=0,
            highlightthickness=0, padx=6, pady=2,
            insertbackground=PAL['caret'], insertwidth=2 if editable else 0,
            selectbackground=PAL['sel'], selectforeground=PAL['fg'],
            tabs=self.font.measure(INDENT))
        t.grid(row=0, column=1, sticky='nsew')
        vs = ttk.Scrollbar(self, orient='vertical', command=t.yview)
        vs.grid(row=0, column=2, sticky='ns')
        self.hs = ttk.Scrollbar(self, orient='horizontal', command=t.xview)
        self.hs.grid(row=1, column=0, columnspan=3, sticky='ew')
        t.configure(yscrollcommand=vs.set, xscrollcommand=self.hs.set)

        self._build_find()
        self._build_bar()
        self._styles()
        self._bind()
        if not editable:
            t.configure(state='disabled')
        self._gutter()

    # ---- gorunum
    def _styles(self):
        c = self.text.tag_configure
        c('cur', background=PAL['curline'])
        for n in self.TAGS:
            c(n, foreground=PAL[n])
        c('comment', foreground=PAL['comment'], font=self.ital)
        c('meta', foreground=PAL['comment'])
        c('errline', background=PAL['errline'])
        c('pair', background=PAL['pair'])
        c('find', background=PAL['find'])
        c('findcur', background=PAL['findcur'])
        self.text.tag_lower('cur')
        for n in ('pair', 'find', 'findcur', 'sel'):
            self.text.tag_raise(n)

    def _build_bar(self):
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, columnspan=3, sticky='ew')
        self.pos = ttk.Label(bar, text="Ln 1, Col 1", width=17, anchor='w')
        self.pos.pack(side='left', padx=(4, 0))
        self.note_lbl = ttk.Label(bar, text="", anchor='w',
                                  foreground=PAL['comment'])
        self.note_lbl.pack(side='left', fill='x', expand=True, padx=4)
        self.wrap = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Sar", variable=self.wrap,
                        command=self.toggle_wrap).pack(side='right')

    def _build_find(self):
        f = self.findbar = ttk.Frame(self)
        f.grid(row=2, column=0, columnspan=3, sticky='ew')
        f.grid_remove()
        ttk.Label(f, text="Bul:").pack(side='left', padx=(4, 2))
        self.qe = ttk.Entry(f, width=30)
        self.qe.pack(side='left')
        self.qe.bind('<KeyRelease>', self._find_now)
        self.qe.bind('<Return>', lambda e: self._step(1))
        self.qe.bind('<Shift-Return>', lambda e: self._step(-1))
        self.qe.bind('<Escape>', lambda e: self.hide_find())
        ttk.Button(f, text="<", width=3,
                   command=lambda: self._step(-1)).pack(side='left', padx=2)
        ttk.Button(f, text=">", width=3,
                   command=lambda: self._step(1)).pack(side='left')
        self.case = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Aa", variable=self.case,
                        command=self._find_now).pack(side='left', padx=4)
        self.fcount = ttk.Label(f, text="", width=12, anchor='w')
        self.fcount.pack(side='left')
        ttk.Button(f, text="Kapat",
                   command=self.hide_find).pack(side='right', padx=2)

    # ---- baglantilar
    def _bind(self):
        t = self.text
        t.bind('<<Edited>>', self._on_edit)
        t.bind('<<Moved>>', self._on_move)
        t.bind('<Configure>', lambda e: self._gutter())
        t.bind('<Expose>', lambda e: self._gutter())
        self.gutter.bind('<Configure>', lambda e: self._gutter())
        t.bind('<Control-f>', lambda e: self.show_find())
        t.bind('<Control-F>', lambda e: self.do_format())
        t.bind('<Escape>', lambda e: self.hide_find())
        t.bind('<Control-a>', self._select_all)
        t.bind('<Control-plus>', lambda e: self.zoom(1))
        t.bind('<Control-equal>', lambda e: self.zoom(1))
        t.bind('<Control-minus>', lambda e: self.zoom(-1))
        t.bind('<Control-MouseWheel>',
               lambda e: self.zoom(1 if e.delta > 0 else -1))
        t.bind('<Button-3>', self._menu)
        if self.editable:
            t.bind('<Return>', self._on_return)
            t.bind('<Tab>', self._on_tab)
            t.bind('<Shift-Tab>', self._on_untab)
            t.bind('<ISO_Left_Tab>', self._on_untab)
            t.bind('<BackSpace>', self._on_bs)
            t.bind('<greater>', self._on_gt)
            t.bind('<slash>', self._on_slash)
            t.bind('<Control-slash>', self._on_comment)
            t.bind('<Control-d>', self._dup_line)
            t.bind('<Control-y>', self._redo)

        self.pop = tk.Menu(self, tearoff=0)
        if self.editable:
            self.pop.add_command(label="Geri al", command=self._undo)
            self.pop.add_command(label="Yinele", command=self._redo)
            self.pop.add_separator()
            self.pop.add_command(
                label="Kes", command=lambda: t.event_generate('<<Cut>>'))
        self.pop.add_command(label="Kopyala",
                             command=lambda: t.event_generate('<<Copy>>'))
        if self.editable:
            self.pop.add_command(
                label="Yapistir",
                command=lambda: t.event_generate('<<Paste>>'))
        self.pop.add_separator()
        self.pop.add_command(label="Bicimle  (Ctrl+Shift+F)",
                             command=self.do_format)
        self.pop.add_command(label="Bul  (Ctrl+F)", command=self.show_find)
        self.pop.add_command(label="Tumunu sec",
                             command=lambda: self._select_all(None))

    def _menu(self, e):
        try:
            self.pop.tk_popup(e.x_root, e.y_root)
        finally:
            self.pop.grab_release()
        return 'break'

    def _undo(self, _e=None):
        try:
            self.text.edit_undo()
        except tk.TclError:
            pass
        return 'break'

    def _redo(self, _e=None):
        try:
            self.text.edit_redo()
        except tk.TclError:
            pass
        return 'break'

    # ---- disari acilan kucuk API (tk.Text ile uyumlu)
    def get(self, a='1.0', b='end-1c'):
        return self.text.get(a, b)

    def insert(self, where, s, *tags):
        return self._rw(self.text.insert, where, s, *tags)

    def delete(self, a, b=None):
        return self._rw(self.text.delete, a, b)

    def edit_reset(self):
        try:
            self.text.edit_reset()
        except tk.TclError:
            pass

    def focus_set(self):
        self.text.focus_set()

    def set_text(self, s, meta=0):
        """Icerigi tumden degistir; meta = ustteki N satir 'baslik' sayilir."""
        self.text.tag_remove('meta', '1.0', 'end')
        self._rw(self.text.delete, '1.0', 'end')
        self._rw(self.text.insert, '1.0', s)
        self.edit_reset()
        self.text.mark_set('insert', '1.0')
        self.text.see('1.0')
        if meta:
            self.text.tag_add('meta', '1.0', '%d.0' % (meta + 1))
        self.highlight_now()

    def _rw(self, fn, *a):
        """Salt-okunur editorde yazma islemi icin state'i gecici ac."""
        if not self.editable:
            self.text.configure(state='normal')
        try:
            return fn(*[x for x in a if x is not None])
        finally:
            if not self.editable:
                self.text.configure(state='disabled')

    def note(self, msg, kind=''):
        self.note_lbl.configure(
            text=msg,
            foreground={'ok': PAL['ok'], 'err': PAL['err']}.get(
                kind, PAL['comment']))

    # ---- olaylar
    def _on_edit(self, _e=None):
        self._gutter()
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(160, self._highlight)

    def _on_move(self, _e=None):
        self._gutter()
        self._curline()
        i = self.text.index('insert').split('.')
        self.pos.configure(text="Ln %s, Col %d" % (i[0], int(i[1]) + 1))
        if self._job is None:
            self._pair()

    def _curline(self):
        self.text.tag_remove('cur', '1.0', 'end')
        if self.editable:
            self.text.tag_add('cur', 'insert linestart', 'insert lineend+1c')

    # ---- satir numaralari
    def _gutter(self):
        g = self.gutter
        g.delete('all')
        t = self.text
        try:
            cur = int(t.index('insert').split('.')[0])
            total = int(t.index('end-1c').split('.')[0])
        except (tk.TclError, ValueError):
            return
        w = g.winfo_width()
        if w < 10:                       # henuz yerlesmemis
            return
        g.create_line(w - 1, 0, w - 1, g.winfo_height(), fill=PAL['sel'])
        i = t.index('@0,0')
        seen = 0
        while True:
            d = t.dlineinfo(i)
            if d is None:
                break
            ln = int(i.split('.')[0])
            if ln != seen:
                seen = ln
                g.create_text(w - 7, d[1], anchor='ne', text=str(ln),
                              font=self.font,
                              fill=PAL['gutter_on'] if ln == cur
                              else PAL['gutter_fg'])
            nxt = t.index('%s+1line linestart' % i)
            if nxt == i:
                break
            i = nxt
        need = max(38, 16 + self.font.measure('0') * len(str(total)))
        if need != int(g.cget('width')):
            g.configure(width=need)

    # ---- renklendirme
    def highlight_now(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self._highlight()

    def _idx(self, off):
        i = bisect.bisect_right(self._starts, off) - 1
        return '%d.%d' % (i + 1, off - self._starts[i])

    def _highlight(self):
        self._job = None
        t = self.text
        src = t.get('1.0', 'end-1c')
        for n in self.TAGS:
            t.tag_remove(n, '1.0', 'end')
        t.tag_remove('errline', '1.0', 'end')
        t.tag_remove('pair', '1.0', 'end')
        self._spans = []
        if not src.strip():
            self.note("")
            return
        if len(src) > MAX_HL:
            self.note("Buyuk belge (%d KB) - renklendirme kapali"
                      % (len(src) // 1024))
            return

        starts = [0]
        p = src.find('\n')
        while p >= 0:
            starts.append(p + 1)
            p = src.find('\n', p + 1)
        self._starts = starts

        buf = {}

        def add(name, a, b):
            if b > a:
                buf.setdefault(name, []).extend((self._idx(a), self._idx(b)))

        for m in _TOK.finditer(src):
            kind = m.lastgroup
            s, e = m.span()
            if kind == 'tag':
                self._paint_tag(src, s, e, add)
            else:
                add(kind, s, e)
        for name, spans in buf.items():
            t.tag_add(name, *spans)

        if self.validate:
            self._check(src)
        self._pair()

    def _paint_tag(self, src, s, e, add):
        raw = src[s:e]
        tail = 2 if raw.endswith('/>') else 1
        add('punct', e - tail, e)
        h = _HEAD.match(raw)
        if not h:
            add('punct', s, s + 1)
            return
        add('punct', s, s + h.end(1))
        pos = h.end(1)
        if h.group(2):
            add('prefix', s + h.start(2), s + h.end(2) + 1)
            pos = h.end(2) + 1
        add('tag', s + pos, s + h.end(3))
        nm = (h.group(2) + ':' if h.group(2) else '') + h.group(3)
        kind = ('close' if raw.startswith('</')
                else 'self' if raw.endswith('/>') else 'open')
        self._spans.append((s, e, kind, nm))
        for a in _ATTR.finditer(raw, h.end(3), len(raw) - tail):
            if a.group(1):
                add('attrprefix', s + a.start(1), s + a.end(1) + 1)
            add('attr', s + a.start(2), s + a.end(2))
            add('punct', s + a.start(3), s + a.end(3))
            add('value', s + a.start(4), s + a.end(4))

    def _check(self, src):
        try:
            etree.fromstring(src.encode('utf-8'),
                             etree.XMLParser(resolve_entities=False,
                                             huge_tree=True))
            self.note("XML gecerli", 'ok')
        except etree.XMLSyntaxError as ex:
            ln = getattr(ex, 'lineno', 0) or 0
            msg = str(ex).split(', line ')[0]
            if ln:
                try:
                    self.text.tag_add('errline', '%d.0' % ln, '%d.end' % ln)
                except tk.TclError:
                    pass
                self.note("Satir %d: %s" % (ln, msg), 'err')
            else:
                self.note(msg, 'err')
        except Exception as ex:
            self.note(str(ex), 'err')

    # ---- acilis/kapanis etiket esleme
    def _pair(self):
        t = self.text
        t.tag_remove('pair', '1.0', 'end')
        if not self._spans:
            return
        off = len(t.get('1.0', 'insert'))
        hit = None
        for k, sp in enumerate(self._spans):
            if sp[0] <= off <= sp[1]:
                hit = k
                break
            if sp[0] > off:
                break
        if hit is None:
            return
        s, e, kind, nm = self._spans[hit]
        mate, depth = None, 0
        if kind == 'open':
            for sp in self._spans[hit + 1:]:
                if sp[3] != nm:
                    continue
                if sp[2] == 'open':
                    depth += 1
                elif sp[2] == 'close':
                    if depth == 0:
                        mate = sp
                        break
                    depth -= 1
        elif kind == 'close':
            for sp in reversed(self._spans[:hit]):
                if sp[3] != nm:
                    continue
                if sp[2] == 'close':
                    depth += 1
                elif sp[2] == 'open':
                    if depth == 0:
                        mate = sp
                        break
                    depth -= 1
        if mate is None:
            return
        try:
            t.tag_add('pair', self._idx(s), self._idx(e))
            t.tag_add('pair', self._idx(mate[0]), self._idx(mate[1]))
        except tk.TclError:
            pass

    # ---- duzenleme kolayliklari
    def _select_all(self, _e):
        self.text.tag_add('sel', '1.0', 'end-1c')
        return 'break'

    def _line_indent(self, line):
        return re.match(r'[ \t]*', line).group()

    def _on_return(self, _e):
        t = self.text
        before = t.get('insert linestart', 'insert')
        after = t.get('insert', 'insert lineend')
        ind = self._line_indent(before)
        last = None
        for last in _TOK.finditer(before):
            pass
        opens = bool(last and last.lastgroup == 'tag'
                     and last.end() == len(before.rstrip())
                     and not last.group().startswith('</')
                     and not last.group().endswith('/>'))
        closes = after.lstrip().startswith('</')
        t.edit_separator()
        if opens and closes:
            t.insert('insert', '\n' + ind + INDENT + '\n' + ind)
            t.mark_set('insert', 'insert-%dc' % (len(ind) + 1))
        elif opens:
            t.insert('insert', '\n' + ind + INDENT)
        else:
            t.insert('insert', '\n' + ind)
        t.see('insert')
        return 'break'

    def _sel_lines(self):
        r = self.text.tag_ranges('sel')
        if not r:
            return None
        a = int(str(r[0]).split('.')[0])
        last = str(r[1])
        b = int(last.split('.')[0])
        if last.endswith('.0') and b > a:
            b -= 1
        return a, b

    def _on_tab(self, _e):
        t = self.text
        rng = self._sel_lines()
        t.edit_separator()
        if rng:
            for ln in range(rng[0], rng[1] + 1):
                t.insert('%d.0' % ln, INDENT)
            t.tag_add('sel', '%d.0' % rng[0], '%d.end' % rng[1])
        else:
            t.insert('insert', INDENT)
        return 'break'

    def _on_untab(self, _e):
        t = self.text
        rng = self._sel_lines() or (int(t.index('insert').split('.')[0]),) * 2
        t.edit_separator()
        for ln in range(rng[0], rng[1] + 1):
            head = t.get('%d.0' % ln, '%d.%d' % (ln, len(INDENT)))
            n = len(head) - len(head.lstrip(' '))
            if n:
                t.delete('%d.0' % ln, '%d.%d' % (ln, n))
        return 'break'

    def _on_bs(self, _e):
        t = self.text
        if t.tag_ranges('sel'):
            return
        before = t.get('insert linestart', 'insert')
        if before and not before.strip() and len(before) % len(INDENT) == 0:
            t.delete('insert-%dc' % len(INDENT), 'insert')
            return 'break'

    def _on_gt(self, _e):
        """'>' yazilinca acik etiketi kendiliginden kapat."""
        t = self.text
        if not t.compare('insert', '==', 'insert lineend'):
            return
        line = t.get('insert linestart', 'insert')
        i = line.rfind('<')
        if i < 0:
            return
        frag = line[i:]
        if frag[1:2] in ('/', '!', '?') or frag.endswith('/') or '>' in frag:
            return
        m = re.match(r'<((?:' + _NM + r':)?' + _NM + r')', frag)
        if not m:
            return
        t.edit_separator()
        t.insert('insert', '></%s>' % m.group(1))
        t.mark_set('insert', 'insert-%dc' % (len(m.group(1)) + 3))
        return 'break'

    def _on_slash(self, _e):
        """'</' yazilinca en yakin acik etiketi tamamla."""
        t = self.text
        head = t.get('1.0', 'insert')
        if not head.endswith('<'):
            return
        st = tag_stack(head[:-1])
        if not st:
            return
        t.edit_separator()
        t.insert('insert', '/%s>' % st[-1])
        return 'break'

    def _on_comment(self, _e):
        t = self.text
        rng = self._sel_lines() or (int(t.index('insert').split('.')[0]),) * 2
        a, b = rng
        blk = t.get('%d.0' % a, '%d.end' % b)
        t.edit_separator()
        s = blk.strip()
        if s.startswith('<!--') and s.endswith('-->'):
            new = blk.replace('<!--', '', 1)
            i = new.rfind('-->')
            new = new[:i] + new[i + 3:]
            new = '\n'.join(x.rstrip() for x in new.splitlines())
        else:
            ind = self._line_indent(blk)
            new = ind + '<!--' + blk[len(ind):] + '-->'
        t.delete('%d.0' % a, '%d.end' % b)
        t.insert('%d.0' % a, new)
        return 'break'

    def _dup_line(self, _e):
        t = self.text
        line = t.get('insert linestart', 'insert lineend')
        t.edit_separator()
        t.insert('insert lineend', '\n' + line)
        return 'break'

    # ---- bicimlendirme
    def do_format(self, quiet=False):
        src = self.get()
        if not src.strip():
            return False
        i = src.find('<')
        if i < 0:
            return False
        head, body = src[:i], src[i:]
        try:
            out = format_xml(body)
        except Exception as ex:
            if not quiet:
                self.note("Bicimlenemedi: %s" % str(ex).split(', line ')[0],
                          'err')
            return False
        self.set_text(head + out, meta=head.count('\n'))
        if not quiet:
            self.note("Bicimlendi.", 'ok')
        return True

    def toggle_wrap(self):
        on = self.wrap.get()
        self.text.configure(wrap='word' if on else 'none')
        if on:
            self.hs.grid_remove()
        else:
            self.hs.grid()
        self._gutter()

    def zoom(self, d):
        self.size = max(7, min(30, self.size + d))
        self.font.configure(size=self.size)
        self.ital.configure(size=self.size)
        self.text.configure(tabs=self.font.measure(INDENT))
        self._gutter()
        return 'break'

    # ---- arama
    def show_find(self):
        self.findbar.grid()
        try:
            sel = self.text.get('sel.first', 'sel.last')
            if sel and '\n' not in sel:
                self.qe.delete(0, 'end')
                self.qe.insert(0, sel)
        except tk.TclError:
            pass
        self.qe.focus_set()
        self.qe.selection_range(0, 'end')
        self._find_now()
        return 'break'

    def hide_find(self):
        self.findbar.grid_remove()
        self.text.tag_remove('find', '1.0', 'end')
        self.text.tag_remove('findcur', '1.0', 'end')
        self._hits = []
        self.text.focus_set()
        return 'break'

    def _find_now(self, _e=None):
        t = self.text
        t.tag_remove('find', '1.0', 'end')
        t.tag_remove('findcur', '1.0', 'end')
        pat = self.qe.get()
        self._hits, self._hit = [], -1
        if not pat:
            self.fcount.configure(text="")
            return
        pos = '1.0'
        while True:
            pos = t.search(pat, pos, stopindex='end',
                           nocase=not self.case.get())
            if not pos:
                break
            end = '%s+%dc' % (pos, len(pat))
            self._hits.append((pos, end))
            t.tag_add('find', pos, end)
            pos = end
        self.fcount.configure(text="%d sonuc" % len(self._hits))
        if self._hits:
            self._step(1, from_cursor=True)

    def _step(self, d, from_cursor=False):
        t = self.text
        if not self._hits:
            return 'break'
        if from_cursor:
            cur = t.index('insert')
            self._hit = 0
            for k, (a, _b) in enumerate(self._hits):
                if t.compare(a, '>=', cur):
                    self._hit = k
                    break
        else:
            self._hit = (self._hit + d) % len(self._hits)
        a, b = self._hits[self._hit]
        t.tag_remove('findcur', '1.0', 'end')
        t.tag_add('findcur', a, b)
        t.see(a)
        self.fcount.configure(text="%d / %d" % (self._hit + 1,
                                                len(self._hits)))
        return 'break'


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=6)
        self.pack(fill='both', expand=True)
        self.client = None
        self.q = queue.Queue()
        self.cur = None
        self.wsdl_url = ''
        self.drafts = {}
        self.store = HistoryStore(HISTORY)
        self._build()
        self._restore()
        self.store.path = self.hist_path
        self.store.load()
        self._fill_history()
        master.protocol('WM_DELETE_WINDOW', self.on_close)
        self._alive = True
        self._tick = self.after(100, self._drain)

    # ---- layout
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill='x')
        ttk.Label(top, text="WSDL:").pack(side='left')
        self.url = ttk.Entry(top)
        self.url.pack(side='left', fill='x', expand=True, padx=4)
        self.url.bind('<Return>', lambda e: self.do_load())
        self.verify = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="SSL dogrula", variable=self.verify)\
            .pack(side='left', padx=4)
        self.btn_load = ttk.Button(top, text="Yukle", command=self.do_load)
        self.btn_load.pack(side='left')

        pw = ttk.Panedwindow(self, orient='horizontal')
        pw.pack(fill='both', expand=True, pady=6)

        # --- sol: operation agaci + gecmis
        left = ttk.Panedwindow(pw, orient='vertical')

        opf = ttk.Labelframe(left, text="Operations", padding=2)
        self.tree = ttk.Treeview(opf, show='tree', selectmode='browse')
        vs = ttk.Scrollbar(opf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.on_pick)
        left.add(opf, weight=3)

        hf = ttk.Labelframe(left, text="Gecmis", padding=2)
        cols = ('ts', 'op', 'st')
        self.hist = ttk.Treeview(hf, columns=cols, show='headings',
                                 selectmode='browse')
        for c, txt, w in (('ts', 'Zaman', 118), ('op', 'Operation', 150),
                          ('st', 'Durum', 52)):
            self.hist.heading(c, text=txt)
            self.hist.column(c, width=w, anchor='w', stretch=(c == 'op'))
        hvs = ttk.Scrollbar(hf, orient='vertical', command=self.hist.yview)
        self.hist.configure(yscrollcommand=hvs.set)
        hvs.pack(side='right', fill='y')
        self.hist.pack(fill='both', expand=True)
        self.hist.bind('<Double-1>', lambda e: self.load_hist())
        hb2 = ttk.Frame(hf)
        hb2.pack(fill='x')
        ttk.Button(hb2, text="Yukle", command=self.load_hist)\
            .pack(side='left')
        ttk.Button(hb2, text="Dosya...", command=self.pick_hist_file)\
            .pack(side='left', padx=2)
        ttk.Button(hb2, text="Temizle", command=self.clear_hist)\
            .pack(side='left')
        left.add(hf, weight=2)
        pw.add(left, weight=1)

        # --- sag: request / response
        right = ttk.Panedwindow(pw, orient='vertical')

        req = ttk.Labelframe(right, text="Request", padding=4)
        bar = ttk.Frame(req)
        bar.pack(fill='x')
        ttk.Label(bar, text="Endpoint:").pack(side='left')
        self.addr = ttk.Entry(bar)
        self.addr.pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(bar, text="Bicimle",
                   command=lambda: self.body.do_format())\
            .pack(side='left', padx=(0, 4))
        self.btn_regen = ttk.Button(bar, text="Sablonu yenile",
                                    command=self.regen, state='disabled')
        self.btn_regen.pack(side='left', padx=(0, 4))
        self.btn_send = ttk.Button(bar, text="Gonder  (Ctrl+Enter)",
                                   command=self.do_send, state='disabled')
        self.btn_send.pack(side='left')

        hb = ttk.Frame(req)
        hb.pack(fill='x', pady=(4, 0))
        ttk.Label(hb, text="Headers:").pack(side='left', anchor='n')
        self.hdrs = tk.Text(hb, height=2, wrap='none', undo=True,
                            font=('Consolas', 9))
        self.hdrs.pack(side='left', fill='x', expand=True, padx=4)

        self.body = XmlEditor(req, editable=True, validate=True)
        self.body.pack(fill='both', expand=True, pady=(4, 0))
        right.add(req, weight=3)

        res = ttk.Labelframe(right, text="Response", padding=4)
        rbar = ttk.Frame(res)
        rbar.pack(fill='x')
        self.autofmt = tk.BooleanVar(value=True)
        ttk.Checkbutton(rbar, text="Yanit geldiginde bicimle",
                        variable=self.autofmt).pack(side='left')
        ttk.Button(rbar, text="Bicimle",
                   command=lambda: self.resp.do_format())\
            .pack(side='left', padx=4)
        ttk.Button(rbar, text="Bul",
                   command=lambda: self.resp.show_find()).pack(side='left')
        self.resp = XmlEditor(res, editable=False)
        self.resp.pack(fill='both', expand=True, pady=(4, 0))
        right.add(res, weight=2)

        pw.add(right, weight=4)

        bot = ttk.Frame(self)
        bot.pack(fill='x')
        self.status = ttk.Label(bot, text="Hazir.", anchor='w')
        self.status.pack(side='left', fill='x', expand=True)
        ttk.Button(bot, text="XML kaydet", command=self.save_req)\
            .pack(side='left')
        ttk.Button(bot, text="XML ac", command=self.open_req)\
            .pack(side='left', padx=4)

        self.master.bind('<Control-Return>', lambda e: self.do_send())

    # ---- ayarlar
    def _restore(self):
        self.hist_path = HISTORY
        try:
            with open(CFG) as f:
                d = json.load(f)
            self.url.insert(0, d.get('url', ''))
            self.verify.set(d.get('verify', False))
            self.hist_path = d.get('history') or HISTORY
        except Exception:
            pass

    def _persist(self):
        try:
            with open(CFG, 'w') as f:
                json.dump({'url': self.url.get(), 'verify': self.verify.get(),
                           'history': self.hist_path}, f)
        except Exception:
            pass

    def say(self, txt):
        self.status.config(text=txt)

    def _drain(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        if self._alive:
            self._tick = self.after(100, self._drain)

    def bg(self, work):
        threading.Thread(target=work, daemon=True).start()

    # ---- editor <-> taslak
    def _snapshot(self):
        return {'body': self.body.get('1.0', 'end').rstrip(),
                'addr': self.addr.get().strip(),
                'headers': self._parse_headers()}

    def _stash(self):
        """Editordeki hali mevcut operation'in taslagi olarak sakla."""
        if self.cur is None:
            return
        txt = self.body.get('1.0', 'end').strip()
        if txt:
            self.drafts[self.cur] = self._snapshot()

    def _show(self, body, addr, headers):
        self.body.set_text(body)
        self.addr.delete(0, 'end')
        self.addr.insert(0, addr or '')
        self.hdrs.delete('1.0', 'end')
        self.hdrs.insert('1.0', "\n".join("%s: %s" % kv
                                          for kv in (headers or {}).items()))
        self.btn_send.config(state='normal')
        self.btn_regen.config(state='normal' if self.cur else 'disabled')

    def _parse_headers(self):
        h = {}
        for line in self.hdrs.get('1.0', 'end').splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                if k.strip():
                    h[k.strip()] = v.strip()
        return h

    # ---- gecmis paneli
    def _fill_history(self):
        self.hist.delete(*self.hist.get_children())
        for x in reversed(self.store.entries):
            st = x['status'] if x['kind'] == 'sent' else 'taslak'
            self.hist.insert('', 'end', iid=str(x['id']),
                             values=(x['ts'].replace('T', ' '),
                                     x['operation'], st))

    def _entry(self, eid):
        for x in self.store.entries:
            if str(x['id']) == str(eid):
                return x
        return None

    def load_hist(self):
        sel = self.hist.selection()
        if not sel:
            return
        x = self._entry(sel[0])
        if not x:
            return
        self._stash()
        key = (x['service'], x['port'], x['operation'])
        self.cur = key if all(key) else self.cur
        self._show(x['envelope'], x['endpoint'], x['headers'])
        self.resp.set_text(x.get('response') or '')
        if x.get('response') and self.autofmt.get():
            self.resp.do_format(quiet=True)
        self.say("Gecmisten yuklendi: %s  (%s)" % (x['operation'], x['ts']))

    def pick_hist_file(self):
        p = filedialog.asksaveasfilename(
            title="Gecmis dosyasi", defaultextension='.xml',
            initialfile=os.path.basename(self.hist_path),
            filetypes=[('XML', '*.xml')], confirmoverwrite=False)
        if not p:
            return
        self.hist_path = p
        self.store.path = p
        self.store.load()
        self._fill_history()
        if self.wsdl_url:
            self.drafts.update(self.store.drafts_for(self.wsdl_url))
        self._persist()
        self.say("Gecmis dosyasi: %s  (%d kayit)"
                 % (p, len(self.store.entries)))

    def clear_hist(self):
        if not messagebox.askyesno(APP, "Gecmis dosyasindaki tum kayitlar "
                                        "silinsin mi?"):
            return
        self.store.entries = []
        self.store.seq = 0
        self.store.flush()
        self._fill_history()
        self.say("Gecmis temizlendi.")

    # ---- WSDL
    def do_load(self):
        url = self.url.get().strip()
        if not url:
            return
        low = url.lower()
        if '?' not in low and not low.endswith(('.wsdl', '.xml')):
            if messagebox.askyesno(APP, "Adresin sonuna ?wsdl eklensin mi?"):
                url += '?wsdl'
                self.url.delete(0, 'end')
                self.url.insert(0, url)
        self._stash()
        self.btn_load.config(state='disabled')
        self.say("WSDL yukleniyor...")
        self._persist()
        v = self.verify.get()

        def work():
            try:
                c = load_wsdl(url, verify=v)
                self.q.put((self._loaded, (c, url, list_operations(c))))
            except Exception as ex:
                self.q.put((self._failed, (ex,)))
        self.bg(work)

    def _loaded(self, client, url, ops):
        self.client = client
        self.wsdl_url = url
        self.btn_load.config(state='normal')
        self.tree.delete(*self.tree.get_children())
        n = 0
        for sname, pname, names in ops:
            pid = self.tree.insert('', 'end', text="%s / %s" % (sname, pname),
                                   open=True, values=('port',))
            for o in names:
                self.tree.insert(pid, 'end', text=o,
                                 values=('op', sname, pname, o))
                n += 1
        # onceki oturumdan kalan taslaklari geri yukle
        old = self.store.drafts_for(url)
        for k, v in old.items():
            self.drafts.setdefault(k, v)
        extra = ("  (%d taslak geri yuklendi)" % len(old)) if old else ""
        self.say("%d operation yuklendi.%s" % (n, extra))

    def _failed(self, ex):
        self.btn_load.config(state='normal')
        self.say("Hata: %s" % ex)
        messagebox.showerror(APP, str(ex))

    def on_pick(self, _evt):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], 'values')
        if not vals or vals[0] != 'op':
            return
        _, sname, pname, oname = vals
        key = (sname, pname, oname)
        if key == self.cur:
            return
        self._stash()                       # onceki operation'i kaybetme
        self.cur = key
        if key in self.drafts:
            d = self.drafts[key]
            self._show(d['body'], d['addr'], d['headers'])
            self.say("%s  (bellekteki hali)" % oname)
            return
        self._new_template(quiet=False)

    def _new_template(self, quiet=True):
        sname, pname, oname = self.cur
        try:
            xml = build_template(self.client, sname, pname, oname)
            addr, hdr = build_endpoint(self.client, sname, pname, oname)
        except Exception as ex:
            self.say("Sablon uretilemedi: %s" % ex)
            if not quiet:
                messagebox.showwarning(APP, "Sablon uretilemedi:\n%s" % ex)
            return
        self._show(xml, addr, hdr)
        self.drafts[self.cur] = self._snapshot()
        self.say("%s hazir." % oname)

    def regen(self):
        if self.cur is None or self.client is None:
            return
        if self.cur in self.drafts and not messagebox.askyesno(
                APP, "Bu operation icin yaptiginiz duzenlemeler silinip "
                     "yeni sablon uretilecek. Devam?"):
            return
        self.drafts.pop(self.cur, None)
        self._new_template()

    # ---- gonderim
    def do_send(self):
        if str(self.btn_send['state']) == 'disabled':
            return
        addr = self.addr.get().strip()
        xml = self.body.get('1.0', 'end').strip()
        if not addr or not xml:
            return
        self._stash()
        hdr = self._parse_headers()
        v = self.verify.get()
        self.btn_send.config(state='disabled')
        self.say("Gonderiliyor...")
        self.resp.set_text('')
        t0 = _dt.datetime.now()

        def work():
            try:
                code, rh, body = post_envelope(addr, hdr, xml, verify=v)
                self.q.put((self._got, (addr, hdr, xml, code, rh, body, t0)))
            except Exception as ex:
                self.q.put((self._sendfail, (addr, hdr, xml, ex, t0)))
        self.bg(work)

    def _record(self, addr, hdr, xml, status, body, t0):
        sn, pn, on = self.cur if self.cur else ('', '', '')
        ms = int((_dt.datetime.now() - t0).total_seconds() * 1000)
        try:
            self.store.add_sent({
                'wsdl': self.wsdl_url, 'service': sn, 'port': pn,
                'operation': on, 'endpoint': addr, 'status': str(status),
                'ms': ms, 'headers': hdr, 'envelope': xml, 'response': body})
            self._fill_history()
        except Exception as ex:
            self.say("Gecmise yazilamadi: %s" % ex)
        return ms

    def _got(self, addr, hdr, xml, code, rh, body, t0):
        self.btn_send.config(state='normal')
        ms = self._record(addr, hdr, xml, code, body, t0)
        self.resp.set_text("HTTP %s   %s\n%s\n%s"
                           % (code, rh.get('Content-Type', ''), '-' * 60,
                              body), meta=2)
        if self.autofmt.get():
            self.resp.do_format(quiet=True)
        self.say("HTTP %s   %d ms   %d byte" % (code, ms, len(body)))

    def _sendfail(self, addr, hdr, xml, ex, t0):
        self.btn_send.config(state='normal')
        msg = "%s: %s" % (type(ex).__name__, ex)
        self._record(addr, hdr, xml, 'ERR', msg, t0)
        self.resp.set_text(msg)
        self.say("Gonderim hatasi.")

    # ---- tek dosya kaydet/ac
    def save_req(self):
        p = filedialog.asksaveasfilename(defaultextension='.xml',
                                         filetypes=[('XML', '*.xml')])
        if p:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(self.body.get('1.0', 'end'))
            self.say("Kaydedildi: %s" % p)

    def open_req(self):
        p = filedialog.askopenfilename(filetypes=[('XML', '*.xml'),
                                                  ('Tumu', '*.*')])
        if p:
            with open(p, encoding='utf-8') as f:
                self.body.set_text(f.read())
            self.btn_send.config(state='normal')
            self.say("Acildi: %s" % p)

    # ---- kapanis
    def on_close(self):
        self._alive = False
        try:
            self.after_cancel(self._tick)
        except Exception:
            pass
        self._stash()
        try:
            if self.wsdl_url and self.drafts:
                self.store.put_drafts(self.wsdl_url, self.drafts)
        except Exception:
            pass
        self._persist()
        self.master.destroy()


def main():
    root = tk.Tk()
    root.title(APP + " - WSDL / SOAP test istemcisi")
    root.geometry("1360x860")
    try:
        ttk.Style().theme_use('vista' if sys.platform == 'win32' else 'clam')
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
