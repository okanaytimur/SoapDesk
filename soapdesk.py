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
import json
import queue
import threading
import datetime as _dt

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

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
        self.btn_regen = ttk.Button(bar, text="Sablonu yenile",
                                    command=self.regen, state='disabled')
        self.btn_regen.pack(side='left', padx=(0, 4))
        self.btn_send = ttk.Button(bar, text="Gonder  (Ctrl+Enter)",
                                   command=self.do_send, state='disabled')
        self.btn_send.pack(side='left')

        hb = ttk.Frame(req)
        hb.pack(fill='x', pady=(4, 0))
        ttk.Label(hb, text="Headers:").pack(side='left', anchor='n')
        self.hdrs = tk.Text(hb, height=2, wrap='none', undo=True)
        self.hdrs.pack(side='left', fill='x', expand=True, padx=4)

        self.body = tk.Text(req, wrap='none', undo=True, font=('Consolas', 10))
        rvs = ttk.Scrollbar(req, orient='vertical', command=self.body.yview)
        self.body.configure(yscrollcommand=rvs.set)
        rvs.pack(side='right', fill='y')
        self.body.pack(fill='both', expand=True, pady=(4, 0))
        right.add(req, weight=3)

        res = ttk.Labelframe(right, text="Response", padding=4)
        self.resp = tk.Text(res, wrap='none', font=('Consolas', 10))
        pvs = ttk.Scrollbar(res, orient='vertical', command=self.resp.yview)
        self.resp.configure(yscrollcommand=pvs.set)
        pvs.pack(side='right', fill='y')
        self.resp.pack(fill='both', expand=True)
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
        self.body.delete('1.0', 'end')
        self.body.insert('1.0', body)
        self.body.edit_reset()
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
        self.resp.delete('1.0', 'end')
        if x.get('response'):
            self.resp.insert('1.0', x['response'])
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
        self.resp.delete('1.0', 'end')
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
        self.resp.insert('1.0', "HTTP %s   %s\n%s\n%s"
                         % (code, rh.get('Content-Type', ''), '-' * 60, body))
        self.say("HTTP %s   %d ms   %d byte" % (code, ms, len(body)))

    def _sendfail(self, addr, hdr, xml, ex, t0):
        self.btn_send.config(state='normal')
        msg = "%s: %s" % (type(ex).__name__, ex)
        self._record(addr, hdr, xml, 'ERR', msg, t0)
        self.resp.insert('1.0', msg)
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
                self.body.delete('1.0', 'end')
                self.body.insert('1.0', f.read())
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
