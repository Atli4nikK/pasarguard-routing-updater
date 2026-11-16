#!/usr/bin/env python3
"""routing-updater: свежий routing-профиль и geo-базы для Happ / INCY на PasarGuard.

Порт идеи https://github.com/3APA3A-3AHO3A/remnawave-routing-updater (MIT) с Remnawave на
официальную PasarGuard (без патчей). Только stdlib Python 3.8+.

Зачем
-----
Happ и INCY получают routing-профиль из заголовка подписки ``routing``
(``happ://routing/onadd/<base64 JSON>``). В профиле есть ссылки ``Geositeurl`` /
``Geoipurl``: клиент скачивает эти базы и по ним разбирает ``geosite:...`` / ``geoip:...`` —
и в правилах профиля, и в Xray JSON, который отдаёт панель. Если база урезана и в ней нет
нужных доменов (например, Ozon в ``geosite:category-ru``), трафик молча уходит в прокси.
Если в базе нет категории вовсе — профиль у клиента не запускается.

Что делает один запуск (``run``)
-------------------------------
    1. Шаблон профиля (JSON) -> список нужных категорий:
         geosite:/geoip: из DirectSites/ProxySites/BlockSites/DirectIp/ProxyIp/BlockIp
         + из Xray JSON-шаблонов клиентов в панели (GET /api/client_templates)
         + RU_EXTRA_GEOSITE / RU_EXTRA_GEOIP.
    2. Скачивает базы-источники (условный запрос с ETag) и собирает свои geosite.dat / geoip.dat
       только из нужных категорий. Источников может быть несколько: одноимённые категории
       сливаются (домены/подсети всех источников), так что категорию, которой нет в основной
       базе (например, WHITELIST из roscomvpn), можно взять из дополнительной.
       Если какой-то нужной категории нет ни в одном источнике — запуск прерывается и панель
       не трогается: такой профиль сломал бы клиентов.
    3. Пишет рядом <file>.sha256 (Happ проверяет базу по нему) и routing.json (autorouting INCY).
    4. LastUpdated меняется только когда изменились базы или шаблон — тогда клиенты перекачают
       базы. Иначе метка прежняя, клиенты ничего не качают.
    5. В настройках подписки PasarGuard (GET/PUT /api/settings, секция subscription) находит
       правило, которое реально сработает для клиента (первое, чей pattern совпадает с его
       User-Agent через re.match — как у панели), и пишет туда заголовки ``routing`` (и
       ``autorouting`` для INCY). Если для клиента срабатывает только общее правило
       (catch-all), создаёт отдельное правило прямо над ним с тем же target. Остальные правила
       и заголовки не трогает. Перед записью сохраняет прежнюю секцию в backups/.

Схема:
    источники баз (GitHub/CDN) --> этот скрипт --> OUTPUT_DIR/{geosite,geoip}.dat, routing.json
                                        |                    ^ nginx отдаёт с домена подписки
                                        +--(API: /api/settings)--> панель --заголовок routing--> клиенты

Команды:
    python3 routing_updater.py run      # один цикл (для systemd timer)
    python3 routing_updater.py check    # всё посчитать и показать план, ничего не записывать
    python3 routing_updater.py restore <backup.json>   # вернуть секцию subscription из backup

Настройки — переменные окружения (см. routing-updater.env.example), ``--env <файл>`` читает их
из файла формата KEY=VALUE.
"""

import argparse
import base64
import copy
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "pasarguard-routing-updater/1.0"

# Клиенты: схема deep-link, пример User-Agent (по нему ищем правило так же, как панель),
# pattern для автосоздаваемого правила, умеет ли клиент autorouting.
CLIENTS = {
    "happ": {
        "scheme": "happ",
        "sample_ua": "Happ/4.8.0/ios",
        "pattern": "^([Hh]app)",
        "autorouting": False,
        "create": True,
    },
    "incy": {
        "scheme": "incy",
        "sample_ua": "INCY/3.9.9/ios",
        "pattern": "^([Ii][Nn][Cc][Yy])",
        "autorouting": True,
        "create": True,
    },
    # Правило под FixErrorVPN владелец создаёт сам (нужен свой target под ядро), мы только
    # обновляем найденное.
    "fixerrorvpn": {
        "scheme": "fixerrorvpn",
        "sample_ua": "Xray-core/25.10.15 FixErrorVPN/1.0 (Windows)",
        "pattern": None,
        "autorouting": True,
        "create": False,
    },
}
# User-Agent, который не похож ни на один клиент: правило, совпавшее с ним, — catch-all.
UNKNOWN_UA = "zz-routing-updater-probe/0"

SITE_FIELDS = ("DirectSites", "ProxySites", "BlockSites")
IP_FIELDS = ("DirectIp", "ProxyIp", "BlockIp")
GEO_FILES = ("geosite.dat", "geoip.dat")


# ============================================================================ настройки

def _bool(value, default=False):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _words(value):
    return [w for w in re.split(r"[\s,]+", value or "") if w]


def load_env_file(path):
    """KEY=VALUE построчно (как EnvironmentFile systemd). Уже заданные переменные не трогаем."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


class Config:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.url = (e.get("PASARGUARD_URL") or "").rstrip("/")
        self.username = e.get("PASARGUARD_USERNAME") or ""
        self.password = e.get("PASARGUARD_PASSWORD") or ""
        self.verify_tls = _bool(e.get("PASARGUARD_VERIFY_TLS"), True)
        self.template_path = e.get("RU_TEMPLATE") or "/etc/pasarguard-routing-updater/template.json"
        self.output_dir = e.get("RU_OUTPUT_DIR") or "/var/lib/pasarguard-routing-updater"
        # Публичный адрес каталога OUTPUT_DIR (nginx). Пусто — базы не зеркалим, клиенты качают
        # по ссылкам из шаблона, autorouting не включаем.
        self.public_base = (e.get("RU_PUBLIC_BASE") or "").rstrip("/")
        self.geosite_sources = _words(e.get("RU_GEOSITE_SOURCES")) or [
            "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"]
        self.geoip_sources = _words(e.get("RU_GEOIP_SOURCES")) or [
            "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"]
        self.clients = [c.lower() for c in _words(e.get("RU_CLIENTS") or "happ incy")]
        self.scan_client_templates = _bool(e.get("RU_SCAN_CLIENT_TEMPLATES"), True)
        self.extra_geosite = {c.upper() for c in _words(e.get("RU_EXTRA_GEOSITE"))}
        self.extra_geoip = {c.upper() for c in _words(e.get("RU_EXTRA_GEOIP"))}
        self.autorouting = _bool(e.get("RU_AUTOROUTING"), True)
        self.create_missing = _bool(e.get("RU_CREATE_MISSING_RULES"), True)
        self.timeout = int(e.get("RU_TIMEOUT") or 60)
        self.keep_backups = int(e.get("RU_KEEP_BACKUPS") or 20)

    @property
    def mirror(self):
        return bool(self.public_base)

    def validate(self):
        problems = []
        if not self.url:
            problems.append("PASARGUARD_URL не задан")
        if not (self.username and self.password):
            problems.append("PASARGUARD_USERNAME / PASARGUARD_PASSWORD не заданы")
        for c in self.clients:
            if c not in CLIENTS:
                problems.append(f"неизвестный клиент в RU_CLIENTS: {c} (есть: {', '.join(CLIENTS)})")
        return problems


# ============================================================================ geo: protobuf
#   GeoSiteList { repeated GeoSite entry = 1 }   GeoSite { string country_code = 1; repeated Domain domain = 2; ... }
#   GeoIPList   { repeated GeoIP entry = 1 }     GeoIP   { string country_code = 1; repeated CIDR cidr = 2; ... }
# Достаточно выбрать записи по country_code и переписать их байты как есть; при слиянии
# одноимённых категорий к записи основного источника дописываются поля 2 из остальных.

def _read_varint(buf, pos):
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _encode_varint(value):
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _iter_fields(buf):
    """(номер поля, wire type, сырые байты поля целиком, payload) для каждого поля сообщения."""
    pos, n = 0, len(buf)
    while pos < n:
        start = pos
        tag, pos = _read_varint(buf, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            _, pos = _read_varint(buf, pos)
            payload = None
        elif wire == 1:
            pos += 8
            payload = None
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            payload = buf[pos:pos + length]
            pos += length
        elif wire == 5:
            pos += 4
            payload = None
        else:
            raise ValueError(f"неподдерживаемый wire type {wire}")
        if pos > n:
            raise ValueError("обрезанный protobuf")
        yield field, wire, buf[start:pos], payload


def parse_entries(data):
    """{CATEGORY: entry_bytes} верхнего уровня (имена — в верхнем регистре)."""
    out = {}
    for field, wire, _raw, payload in _iter_fields(data):
        if field != 1 or wire != 2:
            continue
        code = ""
        for f2, w2, _r2, p2 in _iter_fields(payload):
            if f2 == 1 and w2 == 2:
                code = p2.decode("utf-8", "replace")
                break
        out.setdefault(code.upper(), payload)
    return out


def merge_entry(primary, others):
    """К записи primary дописать поля 2 (домены / подсети) из записей others."""
    extra = bytearray()
    for other in others:
        for field, _wire, raw, _payload in _iter_fields(other):
            if field == 2:
                extra += raw
    return bytes(primary) + bytes(extra)


def entry_size(entry):
    return sum(1 for field, _w, _r, _p in _iter_fields(entry) if field == 2)


def build_dat(sources_data, wanted):
    """Собрать .dat из нужных категорий. sources_data — список bytes (первый — основной).

    Возвращает (bytes, {CATEGORY: число записей}, missing_set).
    """
    parsed = [parse_entries(d) for d in sources_data]
    chunks, sizes, missing = [], {}, set()
    for cat in sorted(wanted):
        found = [p[cat] for p in parsed if cat in p]
        if not found:
            missing.add(cat)
            continue
        entry = merge_entry(found[0], found[1:])
        sizes[cat] = entry_size(entry)
        chunks.append(entry)
    out = bytearray()
    for chunk in chunks:
        out += b"\x0a" + _encode_varint(len(chunk)) + chunk  # поле 1, wire 2
    return bytes(out), sizes, missing


# ============================================================================ категории

_GEO_REF = re.compile(r"\b(geosite|geoip):([A-Za-z0-9_.!@-]+)")


def _category(name):
    return name.split("@", 1)[0].lstrip("!").strip().upper()


def categories_from_template(template):
    site, ip = set(), set()
    for field in SITE_FIELDS + IP_FIELDS:
        for item in template.get(field) or []:
            m = _GEO_REF.search(str(item))
            if m:
                (site if m.group(1) == "geosite" else ip).add(_category(m.group(2)))
    site.discard("")
    ip.discard("")
    return site, ip


def categories_from_text(text):
    """geosite:/geoip: из произвольного текста (Xray JSON-шаблон клиента)."""
    site, ip = set(), set()
    for kind, name in _GEO_REF.findall(text or ""):
        (site if kind == "geosite" else ip).add(_category(name))
    site.discard("")
    ip.discard("")
    return site, ip


# ============================================================================ HTTP

def _ssl_context(verify):
    if verify:
        return None
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class PanelAPI:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ctx = _ssl_context(cfg.verify_tls)
        self.token = None

    def _open(self, method, path, body=None, form=None):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.cfg.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout, context=self.ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"{method} {path}: HTTP {e.code}: {text}") from None
        return json.loads(raw.decode("utf-8")) if raw else None

    def login(self):
        resp = self._open("POST", "/api/admin/token",
                          form={"username": self.cfg.username, "password": self.cfg.password})
        self.token = (resp or {}).get("access_token")
        if not self.token:
            raise RuntimeError("панель не вернула access_token")

    def get_subscription(self):
        settings = self._open("GET", "/api/settings") or {}
        sub = settings.get("subscription")
        if not isinstance(sub, dict) or not isinstance(sub.get("rules"), list):
            raise RuntimeError("в /api/settings нет секции subscription.rules")
        return sub

    def put_subscription(self, sub):
        # PUT принимает секции целиком; отправляем только subscription, прочие (None) не меняются.
        return self._open("PUT", "/api/settings", body={"subscription": sub})

    def client_templates(self):
        resp = self._open("GET", "/api/client_templates?limit=1000") or {}
        return resp.get("templates") or []


def download(url, dest, timeout):
    """Скачать url в dest, если изменился (ETag). True — файл обновлён."""
    etag_path = dest + ".etag"
    headers = {"User-Agent": USER_AGENT}
    if os.path.exists(dest) and os.path.exists(etag_path):
        with open(etag_path, encoding="utf-8") as f:
            etag = f.read().strip()
        if etag:
            headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            new_etag = resp.headers.get("ETag") or ""
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return False
        raise
    if not data:
        raise RuntimeError(f"{url}: пустой ответ")
    parse_entries(data)  # не принимать битый файл
    _atomic_write(dest, data)
    with open(etag_path, "w", encoding="utf-8") as f:
        f.write(new_etag)
    return True


# ============================================================================ файлы

def _atomic_write(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _load_json(path, default):
    raw = _read(path)
    if raw is None:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return default


# ============================================================================ профиль и правила

def template_fingerprint(template):
    """Хэш шаблона без LastUpdated / ссылок на базы: меняется, только если правили сам шаблон."""
    t = {k: v for k, v in template.items() if k not in ("LastUpdated", "Geoipurl", "Geositeurl")}
    return sha256_hex(json.dumps(t, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def decide_stamp(state, geo_hash, tpl_hash, now):
    """(LastUpdated, новое состояние). Метка двигается при первом запуске и при изменениях."""
    new = dict(state)
    if (not state.get("last_updated") or state.get("geo_hash") != geo_hash
            or state.get("template_hash") != tpl_hash):
        new["last_updated"] = str(int(now))
    new["geo_hash"] = geo_hash
    new["template_hash"] = tpl_hash
    return new["last_updated"], new


def encode_profile(profile):
    raw = json.dumps(profile, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def build_headers(client, b64, autorouting_url):
    """Заголовки, которые должны стоять в правиле клиента, и заголовки, которые надо убрать."""
    meta = CLIENTS[client]
    want = {"routing": f"{meta['scheme']}://routing/onadd/{b64}"}
    remove = []
    if meta["autorouting"]:
        if autorouting_url:
            want["autorouting"] = f"{meta['scheme']}://autorouting/onadd/{autorouting_url}"
        else:
            remove.append("autorouting")
    return want, remove


def _matches(pattern, ua):
    try:
        return re.match(pattern, ua) is not None
    except re.error:
        return False


def find_rule(rules, ua):
    for i, rule in enumerate(rules):
        if _matches(rule.get("pattern") or "", ua):
            return i
    return None


def sync_rules(rules, links, create_missing=True):
    """Привести заголовки правил к нужным. Меняет rules на месте, возвращает список строк-итогов.

    links: {client: (want_headers, remove_keys)}.
    """
    summary = []
    for client, (want, remove) in links.items():
        meta = CLIENTS[client]
        idx = find_rule(rules, meta["sample_ua"])
        catch_all = idx is not None and _matches(rules[idx].get("pattern") or "", UNKNOWN_UA)
        if idx is None or catch_all:
            if not (create_missing and meta["create"]):
                summary.append(f"{client}: своего правила нет — пропущено (создайте правило в панели)")
                continue
            target = rules[idx]["target"] if idx is not None else "xray"
            rule = {"pattern": meta["pattern"], "target": target, "response_headers": dict(want)}
            pos = idx if idx is not None else len(rules)
            rules.insert(pos, rule)
            summary.append(f"{client}: создано правило {meta['pattern']} -> {target} (позиция {pos + 1})")
            continue
        headers = rules[idx].setdefault("response_headers", {})
        if headers is None:
            headers = rules[idx]["response_headers"] = {}
        changed = False
        for k, v in want.items():
            if headers.get(k) != v:
                headers[k] = v
                changed = True
        for k in remove:
            if k in headers:
                del headers[k]
                changed = True
        state = "обновлено" if changed else "без изменений"
        summary.append(f"{client}: правило #{idx + 1} {rules[idx].get('pattern')} — {state}")
    return summary


# ============================================================================ цикл

def collect_categories(cfg, template, api):
    site, ip = categories_from_template(template)
    if cfg.scan_client_templates and api is not None:
        for t in api.client_templates():
            if t.get("template_type") == "xray_subscription":
                s, i = categories_from_text(t.get("content") or "")
                site |= s
                ip |= i
    return site | cfg.extra_geosite, ip | cfg.extra_geoip


def build_geo(cfg, site, ip, log):
    """Скачать источники и собрать базы. Возвращает {имя: (bytes, sizes)}; падает при нехватке."""
    cache = os.path.join(cfg.output_dir, ".cache")
    os.makedirs(cache, exist_ok=True)
    result = {}
    for name, sources, wanted in (("geosite.dat", cfg.geosite_sources, site),
                                  ("geoip.dat", cfg.geoip_sources, ip)):
        datas = []
        for n, url in enumerate(sources):
            dest = os.path.join(cache, f"{n}-{name}")
            try:
                if download(url, dest, cfg.timeout):
                    log(f"{name}: источник {n + 1} обновлён")
            except Exception as e:  # сеть: работаем с прошлой копией, если она есть
                if not os.path.exists(dest):
                    raise RuntimeError(f"{name}: не скачать {url}: {e}") from None
                log(f"{name}: источник {n + 1} недоступен ({e}) — беру прошлую копию")
            datas.append(_read(dest))
        data, sizes, missing = build_dat(datas, wanted)
        if missing:
            raise RuntimeError(f"{name}: в источниках нет категорий {', '.join(sorted(missing))} — "
                               "панель не тронута (профиль сломал бы клиентов)")
        empty = sorted(c for c, s in sizes.items() if s == 0)
        if empty:
            log(f"{name}: пустые категории: {', '.join(empty)}")
        result[name] = (data, sizes)
    return result


def run(cfg, apply=True, log=print, now=None):
    import time
    now = time.time() if now is None else now
    template = _load_json(cfg.template_path, None)
    if not isinstance(template, dict):
        raise RuntimeError(f"не прочитать шаблон {cfg.template_path}")
    template.pop("LastUpdated", None)

    api = PanelAPI(cfg)
    api.login()
    sub = api.get_subscription()
    site, ip = collect_categories(cfg, template, api)
    log(f"категории geosite ({len(site)}): {', '.join(sorted(site))}")
    log(f"категории geoip ({len(ip)}): {', '.join(sorted(ip))}")

    os.makedirs(cfg.output_dir, exist_ok=True)
    state_path = os.path.join(cfg.output_dir, ".state.json")
    state = _load_json(state_path, {})

    geo_hash = ""
    if cfg.mirror:
        geo = build_geo(cfg, site, ip, log)
        geo_hash = sha256_hex(b"".join(geo[n][0] for n in GEO_FILES))
        for name in GEO_FILES:
            data, sizes = geo[name]
            log(f"{name}: {len(data)} байт; " + ", ".join(f"{c}={s}" for c, s in sorted(sizes.items())))
        template["Geositeurl"] = f"{cfg.public_base}/geosite.dat"
        template["Geoipurl"] = f"{cfg.public_base}/geoip.dat"

    last_updated, new_state = decide_stamp(state, geo_hash, template_fingerprint(template), now)
    template["LastUpdated"] = last_updated
    b64 = encode_profile(template)
    autorouting_url = f"{cfg.public_base}/routing.json" if (cfg.mirror and cfg.autorouting) else ""
    links = {c: build_headers(c, b64, autorouting_url) for c in cfg.clients}

    new_sub = copy.deepcopy(sub)
    summary = sync_rules(new_sub["rules"], links, cfg.create_missing)
    changed = new_sub != sub
    for line in summary:
        log(line)
    log(f"LastUpdated={last_updated}; панель: {'нужна запись' if changed else 'актуальна'}")

    if not apply:
        log("check: ничего не записано")
        return changed

    if cfg.mirror:
        for name in GEO_FILES:
            data = geo[name][0]
            path = os.path.join(cfg.output_dir, name)
            if _read(path) != data:
                _atomic_write(path, data)
            line = f"{sha256_hex(data)}  {name}\n"
            for ext in (".sha256", ".sha256sum"):
                if _read(path + ext) != line.encode():
                    _atomic_write(path + ext, line)
    _atomic_write(os.path.join(cfg.output_dir, "routing.json"),
                  json.dumps(template, ensure_ascii=False, indent=2))

    if changed:
        backup(cfg, sub)
        api.put_subscription(new_sub)
        log("панель обновлена")
    new_state["applied_at"] = str(int(now))
    _atomic_write(state_path, json.dumps(new_state))
    return changed


def backup(cfg, sub):
    d = os.path.join(cfg.output_dir, "backups")
    os.makedirs(d, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(d, f"subscription-{stamp}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(sub, f, ensure_ascii=False, indent=2)
    old = sorted(p for p in os.listdir(d) if p.startswith("subscription-"))
    for p in old[:-cfg.keep_backups] if cfg.keep_backups > 0 else []:
        os.remove(os.path.join(d, p))
    return path


def restore(cfg, path, log=print):
    sub = _load_json(path, None)
    if not isinstance(sub, dict) or "rules" not in sub:
        raise RuntimeError(f"{path}: это не backup секции subscription")
    api = PanelAPI(cfg)
    api.login()
    backup(cfg, api.get_subscription())
    api.put_subscription(sub)
    log(f"секция subscription восстановлена из {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", help="файл KEY=VALUE с настройками")
    ap.add_argument("command", choices=("run", "check", "restore"))
    ap.add_argument("backup", nargs="?", help="для restore: файл backup")
    args = ap.parse_args(argv)
    if args.env:
        load_env_file(args.env)
    cfg = Config()
    problems = cfg.validate()
    if problems:
        print("FAIL: " + "; ".join(problems), file=sys.stderr)
        return 2
    try:
        if args.command == "restore":
            if not args.backup:
                print("FAIL: укажите файл backup", file=sys.stderr)
                return 2
            restore(cfg, args.backup)
        else:
            run(cfg, apply=args.command == "run")
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
