# routing-updater для PasarGuard

Свежий routing-профиль и geo-базы (`geosite.dat`, `geoip.dat`) для клиентов **Happ**,
**INCY** и **FixErrorVPN** на официальной PasarGuard, без патчей панели. Порт идеи
[remnawave-routing-updater](https://github.com/3APA3A-3AHO3A/remnawave-routing-updater)
(MIT) с Remnawave на API PasarGuard. Нужен только Python 3.8+ (stdlib) и systemd.

## Зачем

Happ и INCY получают routing-профиль из заголовка подписки `routing`
(`happ://routing/onadd/<base64 JSON>`). В профиле лежат ссылки на geo-базы. Клиент
скачивает базы и по ним разбирает `geosite:…` / `geoip:…` — **и в правилах профиля, и в
Xray JSON, который отдаёт панель**. Что может пойти не так:

- база урезана: в `geosite:category-ru` нет Ozon, Wildberries, Госуслуг — их трафик молча
  уходит в прокси, а на нодах, где российский выход закрыт, не открывается совсем;
- в базе нет категории, на которую ссылается профиль или Xray JSON, — профиль у клиента
  не запускается (у INCY — ошибка `EOF`);
- GitHub у клиента недоступен — базы не скачиваются;
- полная база (10–90 МБ) не помещается в память VPN-расширения iOS.

Модуль собирает свои маленькие базы ровно из нужных категорий, отдаёт их с домена
подписки и держит заголовок `routing` в правилах подписки актуальным.

## Как работает

```
источники баз (GitHub/CDN) ──> routing_updater.py ──> RU_OUTPUT_DIR/
                                     │                 routing.json, geosite.dat, geoip.dat, *.sha256
                                     │                       ^ nginx отдаёт с домена подписки
                                     └── API: GET/PUT /api/settings ──> панель ── заголовок routing ──> клиенты
```

Один запуск (`run`, по systemd timer):

1. Категории = `geosite:`/`geoip:` из шаблона профиля + из Xray JSON-шаблонов клиентов в
   панели (`GET /api/client_templates`) + `RU_EXTRA_*`.
2. Скачивает источники (условный запрос с ETag) и собирает базы только из этих категорий.
   Источников может быть несколько: **одноимённые категории сливаются**, поэтому категорию,
   которой нет в основной базе, можно взять из дополнительной. Если категории нет ни в одном
   источнике — запуск прерывается, **панель не трогается**.
3. Пишет `<база>.sha256` (Happ проверяет базу по нему) и `routing.json` (autorouting INCY).
4. `LastUpdated` меняется только при изменении баз или шаблона — тогда клиенты перекачивают
   базы. Иначе панель не переписывается и клиенты ничего не качают.
5. Находит правило подписки, которое реально сработает для клиента (первое, чей `pattern`
   совпадает с его User-Agent через `re.match`, как у панели), и пишет туда `routing`
   (`RU_AUTOROUTING=only` — INCY / FixErrorVPN вместо `routing` получают `autorouting`:
   профиль по ссылке на `routing.json`, INCY сам проверяет его раз в 30 минут; `true` — оба
   заголовка, INCY при этом заводил два профиля; по умолчанию `false`). Если срабатывает только общее правило (catch-all),
   создаёт правило клиента прямо над ним с тем же `target`. Прочие правила и заголовки не
   меняются. Перед записью прежняя секция `subscription` сохраняется в `backups/`.

## Установка

На машине, откуда доступен API панели (обычно сама панель):

```bash
sudo ./panel/routing-updater.sh install
sudo nano /etc/pasarguard-routing-updater/routing-updater.env     # адрес панели, логин, RU_PUBLIC_BASE
sudo nano /etc/pasarguard-routing-updater/template.json           # свой профиль
sudo /usr/local/sbin/pasarguard-routing-updater --env /etc/pasarguard-routing-updater/routing-updater.env check
```

`check` всё посчитает и покажет план (категории, размеры баз, какие правила изменятся),
ничего не записывая. Затем настройте nginx (ниже) и запустите:

```bash
sudo systemctl start pasarguard-routing-updater.service
journalctl -u pasarguard-routing-updater -n 50
sudo ./panel/routing-updater.sh check
```

Клиент применит новый профиль при следующем обновлении подписки.

### nginx

Отдавайте файлы с домена подписки, **только перечисленные имена** (в каталоге лежат ещё
backup секции подписки и кэш — их наружу не отдавать). Блок — в `server { … }` домена
подписки, до общего `location /`:

```nginx
location ~ ^/routing/(routing\.json|geo(site|ip)\.dat(\.sha256(sum)?)?)$ {
    alias /var/lib/pasarguard-routing-updater/$1;
    default_type application/octet-stream;
    types { application/json json; }
    add_header Cache-Control "no-cache" always;
}
```

и `RU_PUBLIC_BASE=https://<домен подписки>/routing`. Проверка:
`nginx -t && systemctl reload nginx`, затем `routing-updater.sh check`.

Не занимайте префикс `/api/` — там API PasarGuard.

## Шаблон профиля

`panel/template.json` — пример. Поля как у Happ/INCY (`DirectSites`, `ProxySites`,
`BlockSites`, `DirectIp`, …, DNS). `LastUpdated` не пишите — его ставит модуль. При
`RU_PUBLIC_BASE` ссылки `Geositeurl`/`Geoipurl` заменяются на ваши.

Любая `geosite:`/`geoip:` категория из шаблона должна быть в источниках, иначе запуск
остановится с перечнем недостающих. Категории, которых нет в основной базе, берите из
дополнительного источника через `RU_GEOSITE_SOURCES="<основная> <дополнительная>"`.

## Откат

```bash
ls /var/lib/pasarguard-routing-updater/backups/
sudo /usr/local/sbin/pasarguard-routing-updater --env /etc/pasarguard-routing-updater/routing-updater.env \
     restore /var/lib/pasarguard-routing-updater/backups/subscription-<время>.json
```

## Удаление

`sudo ./panel/routing-updater.sh uninstall` — убирает скрипт и юниты. Настройки, базы и
backup остаются; заголовок `routing` в правилах панели тоже (он просто больше не обновляется).
