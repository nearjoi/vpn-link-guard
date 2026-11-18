#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Забирает самую свежую ссылку на VPN-подписку из публичного Telegram-канала
через веб-превью t.me/s/<канал> и публикует результат в репозитории:

  index.html  — страничка со свежей ссылкой и кнопкой «Скопировать» (GitHub Pages)
  sub.txt     — свежее содержимое подписки (можно вписать в клиент как URL подписки)
  state.json  — служебное состояние: история ссылок, время проверок

Запускается ТОЛЬКО внутри GitHub Actions. Никаких логинов, api_id, токенов
Telegram и Telethon не нужно: парсится публичная веб-страница канала.

Настройки задаются переменными репозитория (Settings -> Secrets and variables
-> Actions -> Variables):

  TG_CHANNEL   (обязательно) юзернейм канала: @Happ_VPN_official, happvpn или
               ссылка t.me/happvpn
  SUB_MARKER   (необязательно) заголовок секции, после которой лежит ссылка
               подписки. По умолчанию: "Incy | v2box | v2raytun".
               Если пусто — берётся первая ссылка из самого свежего поста
               (универсальный режим для любого канала).
  MAX_PAGES    (необязательно) сколько страниц t.me/s просматривать, по умолчанию 3
  INCLUDE_PATTERN / EXCLUDE_PATTERN (необязательно) regex-фильтры ссылок
"""

import base64
import html as html_lib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------- константы

CONFIG_SCHEMES = (
    "vless", "vmess", "trojan", "ss", "ssr", "hysteria",
    "hysteria2", "tuic", "juicity", "snell", "wireguard",
)
CONFIG_RE = re.compile(
    r"(?m)^\s*(?:%s)://\S+" % "|".join(CONFIG_SCHEMES), re.I
)
SCHEME_LINK_RE = re.compile(
    r"\b(?:%s)://[^\s<>\"'\)\]\},]+" % "|".join(CONFIG_SCHEMES), re.I
)
URL_RE = re.compile(r'https?://[^\s<>"\'\)\]\},]+', re.I)
HREF_RE = re.compile(r'href="(https?://[^"]+)"', re.I)
CODE_RE = re.compile(r"<code>(.*?)</code>", re.S | re.I)

# Заглушка, которой автор помечает недействительные подписки
PLACEHOLDER_RE = re.compile(
    r"(00000000-0000-0000-0000-000000000000|@127\.0\.0\.1)", re.I
)

# Домены, которые никогда не являются ссылкой на подписку
JUNK_DOMAINS = (
    "t.me", "telegram.me", "telegram.org", "telegram.dog", "telesco.pe",
    "telegra.ph", "tgstat.com", "tgstat.ru", "tgram.io",
    "youtube.com", "youtu.be", "google.com", "googleapis.com", "gstatic.com",
    "play.google.com", "apps.apple.com", "apple.com", "microsoft.com",
    "live.com", "office.com", "github.com", "githubusercontent.com",
    "twitter.com", "x.com", "t.co", "instagram.com", "facebook.com",
    "tiktok.com", "vk.com", "ok.ru", "dzen.ru", "ya.ru", "yandex.ru",
    "boosty.to", "patreon.com", "cloudflare.com", "ddos-guard.net",
    "gumroad.com", "paypal.com",
)
MEDIA_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|mp4|m4v|mov|avi|mkv|mp3|ogg|wav|css|js)$",
    re.I,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Серверы подписок (в т.ч. dorog*.ru/exec?url=…) роутят ответ по User-Agent:
# браузерам и python-requests отдают HTML-страницу «Это ссылка для VPN-клиента»,
# а сырой base64/plain конфиг — только VPN-приложениям. Поэтому ссылку подписки
# качаем перебором «приложных» UA (okhttp — HTTP-стек Android-приложений).
SUB_UAS = (
    "okhttp/4.12.0",           # Happ / v2rayNG и др. Android-клиенты
    "sing-box/1.9.0",
    "v2rayNG/1.9.16",
    "v2rayTun/2.1.0",
    "Streisand/1.6.2",
    "Shadowrocket/2.2.40",
    "NekoBox/2.7.6",
    "HiddifyNext/2.0.5",
)

# Страница-заглушка сервера подписки (отличаем её от произвольного HTML).
# Бейдж в ней сообщает статус: "Подписка активна" (класс status-ok) или
# истёк (класс status-expired).
LANDING_RE = re.compile(r"Подписка активна|status-ok|status-expired|VPN-клиента", re.I)
LANDING_ALIVE_RE = re.compile(r"Подписка активна|status-ok", re.I)

MAX_SUB_BYTES = 2_000_000        # ограничение размера ответа подписки
MAX_SEEN = 300                   # сколько «увиденных» ссылок помнить
MAX_HISTORY = 15                 # сколько смен ссылки показывать в истории
MAX_SUB_LINES = 40               # максимум строк в sub.txt для конфигов

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Europe/Astrakhan"))
except Exception:                       # pragma: no cover
    LOCAL_TZ = timezone.utc

# ---------------------------------------------------------------- утилиты


def log(msg: str) -> None:
    print(msg, flush=True)


def summary(msg: str) -> None:
    """Пишет в закладку Summary запуска GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(msg.rstrip() + "\n")
        except Exception:
            pass


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_time(iso: str) -> str:
    """ISO-строка -> 'дд.мм.гггг чч:мм (UTC+X)' по локальной зоне."""
    dt = None
    for parse in (
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ"),
        datetime.fromisoformat,
    ):
        try:
            dt = parse(iso)
            break
        except Exception:
            continue
    if dt is None:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    loc = dt.astimezone(LOCAL_TZ)
    off = loc.strftime("%z")
    off = "UTC+{}:{:02d}".format(int(off[:3]), int(off[3:])) if off else ""
    return loc.strftime("%d.%m.%Y %H:%M") + (" (" + off + ")" if off else "")


def mins_ago(iso: str):
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return int(delta.total_seconds() // 60)
    except Exception:
        return None


def fail(msg: str) -> None:
    summary("**ОШИБКА:** " + msg)
    log("ОШИБКА: " + msg)
    sys.exit(1)


# ---------------------------------------------------------------- конфиг

def get_channel() -> str:
    raw = (os.environ.get("TG_CHANNEL") or "").strip()
    if not raw:
        fail(
            "Не задан канал. Открой Settings -> Secrets and variables -> Actions "
            "-> Variables -> New repository variable: имя TG_CHANNEL, значение — "
            "юзернейм канала (например @Happ_VPN_official)."
        )
    m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]+)", raw)
    name = m.group(1) if m else re.sub(r"[^A-Za-z0-9_]", "", raw)
    if not name:
        fail("Не удалось разобрать юзернейм канала из «%s»." % raw)
    return name


def get_marker():
    """Regex секции, после которой лежит ссылка подписки (или None)."""
    raw = (os.environ.get("SUB_MARKER") or "Incy | v2box | v2raytun").strip()
    if not raw:
        return None
    parts = [re.escape(p.strip()) for p in raw.split("|")]
    return re.compile(r"\s*\|\s*".join(parts), re.I)


def env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- сеть

def http_get(url: str, *, timeout=25, retries=3, ua=None):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": ua or UA, "Accept": "*/*",
                         "Accept-Language": "en,ru;q=0.8"},
                timeout=timeout,
                stream=True,
            )
            chunks, total = [], 0
            for chunk in r.iter_content(65536):
                total += len(chunk)
                if total > MAX_SUB_BYTES:
                    break
                chunks.append(chunk)
            return r.status_code, b"".join(chunks).decode("utf-8", "replace")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    return 0, "network error: %s" % last_err


# ---------------------------------------------------------------- разбор t.me/s

def parse_messages(page_html: str, channel: str) -> list:
    """Возвращает посты: [{id, date, text_block, hrefs, codes, schemes}]."""
    messages = []
    chunks = page_html.split('<div class="tgme_widget_message_wrap')
    for chunk in chunks[1:]:
        m_id = re.search(r'data-post="%s/(\d+)"' % re.escape(channel), chunk)
        if not m_id:
            m_id = re.search(r'data-post="[^"]+/(\d+)"', chunk)
        if not m_id:
            continue
        msg_id = int(m_id.group(1))

        m_date = re.search(r'<time[^>]+datetime="([^"]+)"', chunk)
        date = m_date.group(1) if m_date else None

        idx = chunk.find("tgme_widget_message_text")
        end = chunk.find("tgme_widget_message_footer", idx)
        text_block = chunk[idx: end if end != -1 else len(chunk)] if idx != -1 else ""

        plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", text_block))
        messages.append({
            "id": msg_id,
            "date": date,
            "text_block": text_block,
            "hrefs": [html_lib.unescape(x) for x in HREF_RE.findall(text_block)],
            "codes": [html_lib.unescape(x.strip()) for x in CODE_RE.findall(text_block)],
            "schemes": [x.rstrip(".,;") for x in SCHEME_LINK_RE.findall(plain)],
        })
    return messages


def clean_url(url: str) -> str:
    return html_lib.unescape(url.strip().rstrip(".,;"))


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_junk_url(url: str) -> bool:
    host = host_of(url)
    if not host:
        return True
    for d in JUNK_DOMAINS:
        if host == d or host.endswith("." + d):
            return True
    path = urlparse(url).path or ""
    if MEDIA_EXT_RE.search(path):
        return True
    return False


def urls_from_section(text_block: str, marker_re) -> list:
    """Ссылки-кандидаты из куска поста.

    Если задан marker_re — только текст ПОСЛЕ заголовка секции (Incy | v2box |
    v2raytun). Ссылки достаются из <code>-блоков (как в этом канале), из
    href-атрибутов и из обычного текста.
    """
    tail = text_block
    if marker_re is not None:
        m = marker_re.search(text_block)
        if not m:
            return []
        tail = text_block[m.end():]

    urls = []
    urls += [c for c in CODE_RE.findall(tail)]           # <code>https://…</code>
    urls += HREF_RE.findall(tail)                        # <a href="https://…">
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", tail))
    urls += URL_RE.findall(html_lib.unescape(plain))     # обычный текст
    return urls


def build_candidates(messages: list, marker_re, include_re, exclude_re) -> list:
    """Кандидаты, отсортированные: свежие посты выше, первые ссылки в посте выше.

    В режиме маркера посты без заголовка секции пропускаются — это защита от
    объявлений и рекламных постов без подписки.
    """
    seen_pairs = set()
    candidates = []
    for msg in sorted(messages, key=lambda m: -m["id"]):
        pos = 0
        for raw in urls_from_section(msg["text_block"], marker_re) + msg["schemes"]:
            url = clean_url(raw)
            if not url:
                continue
            if url.lower().startswith(("http://", "https://")):
                if is_junk_url(url):
                    continue
                if include_re and not include_re.search(url):
                    continue
                if exclude_re and exclude_re.search(url):
                    continue
            elif not url.lower().startswith(CONFIG_SCHEMES):
                continue
            key = (msg["id"], url)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            candidates.append({
                "link": url,
                "msg_id": msg["id"],
                "msg_date": msg["date"],
                "pos": pos,
            })
            pos += 1
    return candidates


# ---------------------------------------------------------------- подписка

def try_decode_b64(text: str):
    s = re.sub(r"\s+", "", text)
    if len(s) < 24 or len(s) % 4 == 1 or not re.fullmatch(r"[A-Za-z0-9+/_=-]+", s):
        return None
    pad = "=" * (-len(s) % 4)
    for fn in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            out = fn(s + pad, validate=True).decode("utf-8", "strict")
            if "://" in out:
                return out
        except Exception:
            pass
    return None


def count_configs(text: str):
    """(живых конфигов, всего строк-конфигов). Заглушки не считаются живыми."""
    lines = [ln.strip() for ln in text.splitlines()]
    total = {ln for ln in lines if CONFIG_RE.match(ln)}
    alive = {ln for ln in total if not PLACEHOLDER_RE.search(ln)}
    return len(alive), len(total)


def _classify_response(status: int, content: str):
    """Классифицирует один ответ подписки.

    Возвращает dict с полями kind/servers/http/content/valid/note или
    строку-маркер "LANDING", если сервер отдал страницу-заглушку
    «Это ссылка для VPN-клиента».
    """
    alive, total = count_configs(content)
    if alive > 0:
        if CONFIG_RE.search(content):
            return {"kind": "plain", "servers": alive, "http": status,
                    "content": content, "valid": True, "note": None}
        decoded = try_decode_b64(content)
        if decoded:
            return {"kind": "base64", "servers": alive, "http": status,
                    "content": content, "valid": True, "note": None}
        return {"kind": "unknown", "servers": alive, "http": status,
                "content": None, "valid": False, "note": None}

    # живых конфигов нет
    decoded = try_decode_b64(content)
    if decoded:
        alive_d, total_d = count_configs(decoded)
        if alive_d > 0:
            return {"kind": "base64", "servers": alive_d, "http": status,
                    "content": content, "valid": True, "note": None}
        if total_d > 0:
            return {"kind": "base64", "servers": 0, "http": status,
                    "content": None, "valid": False,
                    "note": "Подписка отдала заглушку «Пользователь не найден» — ссылка уже недействительна"}
    if total > 0:
        return {"kind": "plain", "servers": 0, "http": status,
                "content": None, "valid": False,
                "note": "Подписка отдала заглушку «Пользователь не найден» — ссылка уже недействительна"}
    if content.lstrip().startswith("<"):
        if LANDING_RE.search(content):
            return "LANDING"
        return {"kind": "html", "servers": None, "http": status,
                "content": None, "valid": False,
                "note": "Ссылка отдаёт HTML-страницу, а не файл подписки"}
    return {"kind": "unknown", "servers": None, "http": status,
            "content": None, "valid": False,
            "note": "Ответ ссылки не похож на файл подписки"}


def inspect_subscription(url: str):
    """Скачивает ссылку подписки (перебирая UA VPN-клиентов) и проверяет живость.

    Сервер отдаёт сырой конфиг только «приложным» User-Agent'ам; браузерам —
    страницу-заглушку. Если конфиг получить не удалось, но заглушка говорит
    «Подписка активна» (status-ok) — ссылка считается живой (valid=False,
    потому что содержимое для sub.txt недоступно, но note честно это объясняет).
    """
    landing_alive = False
    landing_dead = False
    statuses = []
    for ua in SUB_UAS:
        status, content = http_get(url, timeout=20, retries=1, ua=ua)
        statuses.append(status)
        if status != 200:
            continue
        res = _classify_response(status, content)
        if res == "LANDING":
            if LANDING_ALIVE_RE.search(content):
                landing_alive = True
            if "status-expired" in content:
                landing_dead = True
            continue
        res["ua"] = ua
        return res

    # ни один UA не дал конфиг
    if landing_alive:
        return {"kind": "landing", "servers": None, "http": 200,
                "content": None, "valid": False,
                "note": ("Сервер подтверждает «Подписка активна», но сам конфиг отдаёт "
                         "только VPN-приложениям (проверил %d клиентских User-Agent). "
                         "Ссылка рабочая — вставь её в приложение напрямую; sub.txt "
                         "оставлен прежним и обновится, когда удастся скачать конфиг."
                         ) % len(SUB_UAS)}
    if landing_dead:
        return {"kind": "landing", "servers": None, "http": 200,
                "content": None, "valid": False,
                "note": "Страница подписки сообщает, что подписка неактивна (истекла) — ссылка уже недействительна"}
    non_ok = [s for s in statuses if s and s != 200]
    if statuses and all(s == 0 for s in statuses):
        return {"kind": "none", "servers": None, "http": 0,
                "content": None, "valid": False,
                "note": "Ссылка не отвечает (сеть недоступна)"}
    if non_ok:
        return {"kind": "none", "servers": None, "http": non_ok[0],
                "content": None, "valid": False,
                "note": "Ссылка не отвечает (HTTP %s) — похоже, она уже недействительна" % non_ok[0]}
    return {"kind": "none", "servers": None, "http": 0,
            "content": None, "valid": False,
            "note": "Ответ ссылки не похож на файл подписки"}


# ---------------------------------------------------------------- состояние

STATE_FILE = "state.json"
SUB_FILE = "sub.txt"
PAGE_FILE = "index.html"


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"channel": None, "current": None, "history": [], "seen": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- страница

PAGE_TMPL = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Актуальная ссылка на VPN-подписку</title>
<style>
:root{--bg:#0f1420;--card:#171e2e;--line:#26304a;--text:#e8ecf6;--muted:#8b96ad;--acc:#4f8cff;--ok:#2fbf71;--warn:#ffb64f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:780px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.35rem;margin:.2rem 0 1rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}
.linkbox{display:flex;gap:8px;flex-wrap:wrap;align-items:stretch}
.link{flex:1 1 320px;min-width:0;word-break:break-all;background:#0b101b;border:1px solid var(--line);border-radius:10px;padding:12px;font-size:.95rem}
button{background:var(--acc);color:#fff;border:0;border-radius:10px;padding:12px 18px;font-size:1rem;cursor:pointer}
button:active{transform:scale(.98)}
.muted{color:var(--muted);font-size:.88rem}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.grid div{background:#0b101b;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:.9rem}
.grid b{display:block;font-size:1.02rem;color:var(--text)}
.badge{display:inline-block;border-radius:999px;padding:3px 12px;font-size:.82rem;border:1px solid var(--warn);color:var(--warn)}
.okbadge{display:inline-block;border-radius:999px;padding:3px 12px;font-size:.82rem;border:1px solid var(--ok);color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:.86rem}
td,th{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
td.l{word-break:break-all;max-width:340px}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:var(--ok);color:#04140b;padding:10px 18px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none;font-weight:600}
#toast.show{opacity:1}
a{color:var(--acc)}
.small{font-size:.82rem}
</style>
</head>
<body>
<main>
<h1>Актуальная ссылка на VPN-подписку</h1>
<p class="muted">Канал: <a href="https://t.me/s/__CHANNEL__">t.me/__CHANNEL__</a> · проверяется GitHub Actions каждый час</p>

<div class="card">
__ALERT__
<div class="linkbox">
<div class="link" id="lnk">__LINK__</div>
<button onclick="copyLink()">Скопировать</button>
</div>
<p class="muted small">Скопируй и вставь в Incy / v2box / v2raytun / Happ как подписку (Subscription URL).</p>
</div>

<div class="grid">
<div>Сообщение<b><a href="__POST_URL__">#__MSG_ID__</a> · __MSG_DATE__</b></div>
<div>Серверов в подписке<b>__SERVERS__</b></div>
<div>Ссылка актуальна с<b>__SINCE__</b></div>
<div>Проверено<b>__CHECKED__</b></div>
</div>

<div class="card">
<b>Файл sub.txt (для клиента, вставить один раз)</b>
<div class="linkbox" style="margin-top:8px">
<div class="link small" id="suburl">__SUB_URL__</div>
<button onclick="copySub()">Скопировать</button>
</div>
<p class="muted small">Содержимое sub.txt обновляется автоматически и всегда повторяет свежую подписку автора.</p>
</div>

__EXTRA__

<div class="card">
<b>История смен ссылок</b>
__HISTORY__
</div>

<div class="card muted small">
<b>Как это работает.</b> Каждый час GitHub Actions открывает публичную страницу
канала t.me/s/__CHANNEL__, находит в самом свежем посте секцию с подпиской
(Incy | v2box | v2raytun), забирает ссылку оттуда, скачивает её и проверяет на
живость. Ничего не запускается на твоём компьютере или телефоне.
Если нужно проверить прямо сейчас: Actions → Update VPN subscription → Run workflow.
</div>
</main>
<div id="toast">Скопировано</div>
<script>
const LINK = document.getElementById('lnk').textContent.trim();
const SUBURL = document.getElementById('suburl').textContent.trim();
function toast(){const t=document.getElementById('toast');t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1600);}
async function copyLink(){try{await navigator.clipboard.writeText(LINK);}catch(e){const ta=document.createElement('textarea');ta.value=LINK;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();}toast();}
async function copySub(){try{await navigator.clipboard.writeText(SUBURL);}catch(e){const ta=document.createElement('textarea');ta.value=SUBURL;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();}toast();}
</script>
</body>
</html>
"""


def esc(s):
    return html_lib.escape(str(s)) if s is not None else "—"


def render_page(state, repo_slug):
    cur = state.get("current") or {}
    channel = state.get("channel") or "unknown"
    sub_url = "https://raw.githubusercontent.com/%s/main/sub.txt" % repo_slug

    if cur.get("note"):
        alert = '<p class="badge">%s</p>' % esc(cur["note"])
    else:
        alert = ('<p class="okbadge">Ссылка проверена: подписка отвечает, '
                 'конфигов внутри: %s</p>' % esc(cur.get("servers")))

    extra = ""
    others = [l for l in (cur.get("extra_links") or []) if l != cur.get("link")]
    if others:
        rows = "".join("<div class='link small' style='margin-top:6px'>%s</div>" % esc(l) for l in others)
        extra = ("<div class='card'><b>Другие ссылки из этого же поста</b>%s"
                 "<p class='muted small'>Если основная ссылка не подошла — попробуй эти.</p></div>" % rows)

    if state.get("history"):
        rows = "".join(
            "<tr><td>%s</td><td class='l'>%s</td><td><a href='https://t.me/%s/%d'>#%d</a></td><td>%s</td></tr>"
            % (esc(fmt_time(h["since_utc"]) if h.get("since_utc") else ""),
               esc((h.get("link") or "")[:90]),
               esc(channel), h.get("message_id", 0), h.get("message_id", 0),
               esc(h.get("servers") if h.get("servers") is not None else "—"))
            for h in state["history"]
        )
        history = ("<table><tr><th>Стала актуальной</th><th>Ссылка</th><th>Пост</th>"
                   "<th>Серверов</th></tr>%s</table>" % rows)
    else:
        history = "<p class='muted small'>Пока ссылка не менялась.</p>"

    checked_iso = cur.get("checked_utc") or state.get("last_run_utc")
    checked = fmt_time(checked_iso) if checked_iso else "—"
    ag = mins_ago(checked_iso) if checked_iso else None
    if ag is not None:
        checked += " · %d мин назад" % ag

    page = (PAGE_TMPL
            .replace("__CHANNEL__", esc(channel))
            .replace("__ALERT__", alert)
            .replace("__LINK__", esc(cur.get("link") or "—"))
            .replace("__POST_URL__", "https://t.me/%s/%s" % (esc(channel), esc(cur.get("message_id") or 0)))
            .replace("__MSG_ID__", esc(cur.get("message_id") or "—"))
            .replace("__MSG_DATE__", esc(fmt_time(cur["msg_date"]) if cur.get("msg_date") else "—"))
            .replace("__SERVERS__", esc(cur.get("servers") if cur.get("servers") is not None else "—"))
            .replace("__SINCE__", esc(fmt_time(cur["became_current_utc"]) if cur.get("became_current_utc") else "—"))
            .replace("__CHECKED__", checked)
            .replace("__SUB_URL__", esc(sub_url))
            .replace("__EXTRA__", extra)
            .replace("__HISTORY__", history))
    with open(PAGE_FILE, "w", encoding="utf-8") as f:
        f.write(page)


# ---------------------------------------------------------------- sub.txt

def build_sub_txt(chosen, info, state, candidates):
    """Возвращает СТРОКУ для sub.txt или None (файл не трогать)."""
    link = chosen["link"]
    if link.lower().startswith(("http://", "https://")):
        if info.get("valid") and info.get("content"):
            return info["content"].rstrip() + "\n"
        return None  # не скачали / заглушка — оставить предыдущий файл
    # конфиг-ссылки (vless:// и т.п.): свежая сверху + предыдущие
    lines = [link]
    for c in candidates:
        l = c["link"]
        if l not in lines and not l.lower().startswith(("http://", "https://")):
            lines.append(l)
        if len(lines) >= MAX_SUB_LINES:
            break
    return "\n".join(lines[:MAX_SUB_LINES]) + "\n"


# ---------------------------------------------------------------- main

def main():
    channel = get_channel()
    marker_re = get_marker()
    max_pages = env_int("MAX_PAGES", 3)
    include = (os.environ.get("INCLUDE_PATTERN") or "").strip()
    exclude = (os.environ.get("EXCLUDE_PATTERN") or "").strip()
    include_re = re.compile(include, re.I) if include else None
    exclude_re = re.compile(exclude, re.I) if exclude else None
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")

    log("Канал: t.me/%s ; страниц: %d ; маркер: %s"
        % (channel, max_pages, "да" if marker_re else "нет (первая ссылка поста)"))

    # 1. читаем канал
    messages, seen_ids = [], set()
    before = None
    for page in range(max_pages):
        url = "https://t.me/s/%s" % channel + ("?before=%d" % before if before else "")
        status, body = http_get(url, timeout=25, retries=3)
        if status != 200:
            fail("t.me вернул HTTP %s для t.me/s/%s. Проверь, что канал публичный и юзернейм верный."
                 % (status, channel))
        msgs = parse_messages(body, channel)
        if not msgs and page == 0:
            if "tgme_page_title" not in body:
                fail("Канал t.me/%s не найден (или закрыт). Проверь переменную TG_CHANNEL." % channel)
            break
        for m in msgs:
            if m["id"] not in seen_ids:
                seen_ids.add(m["id"])
                messages.append(m)
        if not msgs:
            break
        before = min(m["id"] for m in msgs)
        if page < max_pages - 1:
            time.sleep(1.5)

    log("Постов прочитано: %d" % len(messages))

    # 2. кандидаты
    candidates = build_candidates(messages, marker_re, include_re, exclude_re)
    log("Ссылок-кандидатов найдено: %d" % len(candidates))

    state = load_state()
    state["channel"] = channel
    state["last_run_utc"] = now_utc()

    if not candidates:
        marker_name = (os.environ.get("SUB_MARKER") or "Incy | v2box | v2raytun").strip() or "маркер"
        if state.get("current"):
            log("В свежих постах нет секции с подпиской — сохраняем предыдущую ссылку.")
            summary("Секция «%s» в последних %d постах не найдена. Текущая ссылка (пост #%s) остаётся актуальной."
                    % (marker_name, len(messages), state["current"].get("message_id") or "?"))
            save_state(state)
            sys.exit(0)
        fail("В последних %d постах канала не найдена секция «%s». Возможно, автор изменил формат "
             "поста — пришли актуальный текст заголовка секции и поправь переменную SUB_MARKER."
             % (len(messages), marker_name))

    # 3. выбор текущей ссылки: свежий пост важнее старого
    ordered = candidates
    new_links = [c for c in ordered if c["link"] not in state["seen"]]
    if new_links:
        chosen, reason = new_links[0], "новая ссылка (пост #%d)" % new_links[0]["msg_id"]
    else:
        chosen, reason = ordered[0], "самый свежий пост с ссылкой (#%d)" % ordered[0]["msg_id"]
    log("Выбрана ссылка: %s" % chosen["link"])
    log("Причина: %s" % reason)

    changed = (state.get("current") or {}).get("link") != chosen["link"]

    # 4. проверка содержимого / сборка sub.txt
    if chosen["link"].lower().startswith(("http://", "https://")):
        info = inspect_subscription(chosen["link"])
    else:
        info = {"kind": "config", "servers": None, "http": 0, "valid": True,
                "content": chosen["link"], "note": None}

    sub_content = build_sub_txt(chosen, info, state, candidates)
    if sub_content is not None:
        with open(SUB_FILE, "w", encoding="utf-8") as f:
            f.write(sub_content)
        log("sub.txt обновлён (%d байт)" % len(sub_content))
    elif info.get("note"):
        log("sub.txt НЕ изменён: %s" % info["note"])

    # 5. обновляем состояние
    same_msg_links = [c["link"] for c in ordered if c["msg_id"] == chosen["msg_id"]]
    if changed:
        if state.get("current"):
            state["history"].insert(0, {
                "link": state["current"].get("link"),
                "message_id": state["current"].get("message_id"),
                "msg_date": state["current"].get("msg_date"),
                "since_utc": state["current"].get("became_current_utc"),
                "servers": state["current"].get("servers"),
            })
            state["history"] = state["history"][:MAX_HISTORY]
        state["current"] = {
            "link": chosen["link"],
            "message_id": chosen["msg_id"],
            "msg_date": chosen["msg_date"],
            "became_current_utc": now_utc(),
            "kind": info.get("kind"),
            "servers": info.get("servers"),
            "http_status": info.get("http"),
            "note": info.get("note"),
            "extra_links": same_msg_links,
            "checked_utc": now_utc(),
        }
    else:
        state["current"].update({
            "kind": info.get("kind"),
            "servers": info.get("servers"),
            "http_status": info.get("http"),
            "note": info.get("note"),
            "extra_links": same_msg_links,
            "checked_utc": now_utc(),
        })

    state["seen"][chosen["link"]] = {"message_id": chosen["msg_id"], "first_seen_utc": now_utc()}
    for c in new_links:
        state["seen"].setdefault(c["link"], {"message_id": c["msg_id"], "first_seen_utc": now_utc()})
    while len(state["seen"]) > MAX_SEEN:
        oldest = min(state["seen"], key=lambda k: state["seen"][k].get("first_seen_utc", ""))
        del state["seen"][oldest]

    save_state(state)
    render_page(state, repo_slug)

    # 6. отчёт в Summary запуска
    cur = state["current"]
    summary(
        "**Канал:** t.me/%s\n\n"
        "**Текущая ссылка:** `%s`\n\n"
        "**Пост:** #%s (%s) · **серверов в подписке:** %s · **проверено:** %s\n\n%s"
        % (channel, cur["link"], cur.get("message_id"),
           fmt_time(cur["msg_date"]) if cur.get("msg_date") else "—",
           cur.get("servers") if cur.get("servers") is not None else "—",
           fmt_time(cur.get("checked_utc") or ""),
           ("⚠️ " + cur["note"]) if cur.get("note") else "Подписка живая.")
    )
    log("Готово.")


if __name__ == "__main__":
    main()
