#!/usr/bin/env python3
"""Build the live site from the design template, a Google Sheet and #блог posts of the channel.

Rule number one: bad input never breaks the site. A source that cannot be read or validated keeps
its last good data (data/state.json) and the reason appears in Russian on status.html.
Standard library only. Runs in GitHub Actions and locally.

  python3 src/sync.py --root .                       # live: network
  python3 src/sync.py --root . --fixtures DIR        # offline: DIR/prices.csv, pages, images
"""
import argparse, csv, datetime as dt, hashlib, html, io, json, re, shutil, sys, urllib.parse, urllib.request
from html.parser import HTMLParser
from pathlib import Path

MSK = dt.timezone(dt.timedelta(hours=3))
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
UA = "Mozilla/5.0 (compatible; site-sync)"
NB = "\xa0"


class SheetError(Exception):
    """A message for the client, in Russian."""


# ---------------------------------------------------------------- fetching
class NetFetcher:
    def __init__(self, cfg):
        self.cfg = cfg

    def _get(self, url, limit=4_000_000):
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise ValueError("response too large")
            return data, r.headers.get("Content-Type", "")

    def sheet(self, name):
        if not self.cfg.get("sheet_id"):
            raise SheetError("таблица ещё не подключена")
        url = f"https://docs.google.com/spreadsheets/d/{self.cfg['sheet_id']}/export?format=csv&gid={self.cfg['gids'][name]}"
        data, ctype = self._get(url)
        if "csv" not in ctype:
            raise SheetError("таблица не открывается по ссылке")
        return data.decode("utf-8-sig")

    def page(self, path):
        return self._get("https://t.me/" + path)[0].decode("utf-8", "replace")

    def image(self, url):
        return self._get(url, limit=3_000_000)[0]


class FileFetcher:
    """Offline fetcher for tests: a missing file behaves like a network error."""
    def __init__(self, d):
        self.d = Path(d)

    def sheet(self, name):
        return (self.d / f"{name}.csv").read_text(encoding="utf-8-sig")

    def page(self, path):
        key = re.sub(r"[^\w]+", "_", urllib.parse.unquote(path)).strip("_")
        return (self.d / f"{key}.html").read_text(encoding="utf-8")

    def image(self, url):
        return (self.d / url.rsplit("/", 1)[-1]).read_bytes()


# ---------------------------------------------------------------- sheet validation
def rows_of(text, required, label):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SheetError(f"лист «{label}» пустой")
    head = [h.strip() for h in rows[0]]
    missing = [c for c in required if c not in head]
    if missing:
        raise SheetError(f"лист «{label}»: пропал столбец «{missing[0]}». Верните прежнее название в первой строке")
    out = []
    for n, r in enumerate(rows[1:], start=2):
        rec = {h: (r[i].strip(" \t\r\n") if i < len(r) else "") for i, h in enumerate(head)}
        if any(rec[c] for c in required if c != "Показывать"):
            rec["_n"] = n
            out.append(rec)
    return out


def shown(rec, label):
    v = rec.get("Показывать", "").strip().lower()
    if v in ("", "да", "yes", "true", "1", "+"):
        return True
    if v in ("нет", "no", "false", "0", "-"):
        return False
    raise SheetError(f"лист «{label}», строка {rec['_n']}: в столбце «Показывать» напишите «да» или «нет»")


def limit(rec, col, size, label, required=False):
    v = rec.get(col, "")
    if required and not v:
        raise SheetError(f"лист «{label}», строка {rec['_n']}: пустой столбец «{col}»")
    if len(v) > size:
        raise SheetError(f"лист «{label}», строка {rec['_n']}: «{col}» длиннее {size} знаков, сократите")
    return v


def price(rec, col, label, required):
    v = re.sub(r"(?i)руб\.?|₽|р\.|[\s\xa0 ]", "", rec.get(col, ""))
    if not v:
        if required:
            raise SheetError(f"лист «{label}», строка {rec['_n']}: пустая цена")
        return None
    if not v.isdigit() or not 100 <= int(v) <= 1_000_000:
        raise SheetError(f"лист «{label}», строка {rec['_n']}: цена «{rec[col]}» не похожа на цену. Пишите число от 100 до 1 000 000")
    return int(v)


def parse_prices(text, core):
    L = "Цены"
    cols = ["Код", "Заголовок", "Название формата", "Описание", "Для кого", "Цена 1, ₽", "Пояснение к цене 1",
            "Цена 2, ₽", "Пояснение к цене 2", "Метка", "Текст кнопки", "Сообщение в Telegram", "Показывать"]
    out, codes = [], set()
    for r in rows_of(text, cols, L):
        code = re.sub(r"[^a-z0-9-]", "", r["Код"].lower()) or f"row{r['_n']}"
        if code in codes:
            raise SheetError(f"лист «{L}», строка {r['_n']}: такой «Код» уже есть. Очистите ячейку «Код»")
        codes.add(code)
        if not shown(r, L):
            if code in core:
                raise SheetError(f"лист «{L}», строка {r['_n']}: этот формат нужен квизу, скрыть его нельзя")
            continue
        p1 = price(r, "Цена 1, ₽", L, code in core)
        p2 = price(r, "Цена 2, ₽", L, False)
        title = limit(r, "Заголовок", 70, L, True)
        out.append({"code": code, "title": title, "name": limit(r, "Название формата", 60, L, True).rstrip("."),
                    "desc": limit(r, "Описание", 280, L), "for": limit(r, "Для кого", 240, L),
                    "p1": p1, "l1": limit(r, "Пояснение к цене 1", 40, L), "p2": p2, "l2": limit(r, "Пояснение к цене 2", 40, L),
                    "tag": limit(r, "Метка", 20, L), "button": limit(r, "Текст кнопки", 30, L) or "Написать мне",
                    "msg": limit(r, "Сообщение в Telegram", 220, L) or f"Здравствуйте, Эля! Интересует формат «{title.strip('«»')}»."})
    missing = [c for c in core if c not in codes]
    if missing:
        raise SheetError(f"лист «{L}»: удалена строка с кодом «{missing[0]}», она нужна квизу. Верните её через «Файл → История версий»")
    return out


def parse_reviews(text):
    L = "Отзывы"
    cols = ["Имя", "Должность", "Запрос и результат", "Отзыв", "Тема", "Показывать"]
    themes = {"работа и деньги": "career", "состояние": "state", "": "other"}
    out = []
    for r in rows_of(text, cols, L):
        if not shown(r, L):
            continue
        t = r["Тема"].strip().lower()
        if t not in themes:
            raise SheetError(f"лист «{L}», строка {r['_n']}: тема бывает «Работа и деньги», «Состояние» или пустая")
        out.append({"name": limit(r, "Имя", 40, L, True), "role": limit(r, "Должность", 110, L),
                    "result": limit(r, "Запрос и результат", 160, L), "text": limit(r, "Отзыв", 700, L, True), "theme": themes[t]})
    if not out:
        raise SheetError(f"лист «{L}»: ни одного отзыва для показа. Сайт оставил прежние")
    return out


def parse_date(v, n, label):
    for f in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(v, f).date().isoformat()
        except ValueError:
            pass
    raise SheetError(f"лист «{label}», строка {n}: дату пишите так: 26.11.2026")


def parse_manual(text, channel):
    L = "Блог"
    cols = ["Дата", "Заголовок", "Текст", "Ссылка на пост в канале", "Показывать"]
    out = []
    for r in rows_of(text, cols, L):
        link = r["Ссылка на пост в канале"]
        m = re.fullmatch(rf"(?:https?://)?t\.me/{re.escape(channel)}/(\d+)/?(?:\?.*)?", link) if link else None
        if link and not m:
            raise SheetError(f"лист «{L}», строка {r['_n']}: ссылка должна вести на пост вашего канала (t.me/{channel}/…)")
        if not shown(r, L):
            if m:
                out.append({"id": m.group(1), "hidden": True})
            continue
        title = limit(r, "Заголовок", 120, L, True)
        body = limit(r, "Текст", 20000, L, True)
        date = parse_date(r["Дата"], r["_n"], L)
        pid = m.group(1) if m else "t-" + date.replace("-", "") + "-" + hashlib.sha1(title.encode()).hexdigest()[:6]
        paras = "".join(f"<p>{html.escape(p.strip()).replace(chr(10), '<br>')}</p>" for p in re.split(r"\n\s*\n", body) if p.strip())
        out.append({"id": pid, "date": date, "title": title, "html": paras, "plain": re.sub(r"\s+", " ", body)[:200],
                    "link": f"https://t.me/{channel}/{m.group(1)}" if m else "", "images": [], "source": "sheet"})
    return out


# ---------------------------------------------------------------- channel
class Messages(HTMLParser):
    """Collect per message: text as a small safe HTML subset, date, photo ids and urls, video flag."""
    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr"}

    def __init__(self, channel):
        super().__init__(convert_charrefs=True)
        self.channel, self.posts, self.cur, self.depth, self.text_at = channel, {}, None, 0, None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        post = a.get("data-post", "")
        if post.lower().startswith(self.channel.lower() + "/"):
            pid = post.split("/", 1)[1]
            if pid.isdigit():
                self.cur = self.posts.setdefault(pid, {"id": pid, "text": [], "date": "", "images": [], "video": False})
        if self.cur is None:
            return
        if "tgme_widget_message_photo_wrap" in cls:
            m = re.search(r"background-image:url\('([^']+)'\)", a.get("style") or "")
            key = "-".join(c for c in cls if re.fullmatch(r"[\d_]+", c)) or str(len(self.cur["images"]))
            if m:
                self.cur["images"].append({"key": re.sub(r"[^\d_-]", "", key)[:60], "url": m.group(1)})
        if any(c.startswith("tgme_widget_message_video") for c in cls):
            self.cur["video"] = True
        if tag == "time" and a.get("datetime") and "time" in cls:
            self.cur["date"] = a["datetime"][:10]
        if tag == "div" and "js-message_text" in cls and self.text_at is None:
            self.text_at, self.stack = 0, []
            return
        if self.text_at is None:
            return
        if tag == "br":
            self.cur["text"].append(("br",))
            return
        if tag in self.VOID:
            return
        self.text_at += 1
        close = None
        if tag in ("b", "strong") and "emoji" not in cls:
            self.cur["text"].append(("raw", "<strong>")); close = "</strong>"
        elif tag == "i" and "emoji" not in cls:
            self.cur["text"].append(("raw", "<em>")); close = "</em>"
        elif tag == "a":
            href = a.get("href") or ""
            if re.match(r"https?://", href):
                self.cur["text"].append(("raw", f'<a href="{html.escape(href, quote=True)}" rel="noopener nofollow">')); close = "</a>"
        self.stack.append((tag, close))

    def handle_endtag(self, tag):
        if self.text_at is None or tag in self.VOID:
            return
        if self.text_at == 0:
            if tag == "div":
                self.text_at = None
            return
        self.text_at -= 1
        if self.stack:
            _, close = self.stack.pop()
            if close:
                self.cur["text"].append(("raw", close))

    def handle_data(self, data):
        if self.text_at is not None and self.cur is not None:
            self.cur["text"].append(("t", data))


def messages(page, channel):
    p = Messages(channel)
    p.feed(page)
    p.close()
    return p.posts


def plain_of(tokens):
    return "".join(t[1] if t[0] == "t" else ("\n" if t[0] == "br" else "") for t in tokens)


def has_tag(tokens, tag):
    return re.search(re.escape(tag) + r"(?![\w])", plain_of(tokens), re.I) is not None


def make_post(m, tag, channel):
    pat = re.compile(re.escape(tag) + r"(?![\w])", re.I)
    lines = [[]]
    for t in m["text"]:
        if t[0] == "br":
            lines.append([])
        else:
            lines[-1].append(("t", pat.sub("", t[1])) if t[0] == "t" else t)
    plain = lambda ln: "".join(t[1] for t in ln if t[0] == "t").strip()
    while lines and not plain(lines[0]):
        lines.pop(0)
    if not lines:
        title, rest = "Запись из канала", []
    else:
        first = plain(lines[0])
        if len(first) <= 90:
            title, rest = first.rstrip(" ."), lines[1:]
        else:
            cut = re.match(r"(.{20,88}?[.!?…])\s", first)
            title = cut.group(1).rstrip(".") if cut else first[:88].rsplit(" ", 1)[0] + "…"
            rest = lines
    paras, cur = [], []
    for ln in rest + [[]]:
        if plain(ln):
            cur.append("".join(html.escape(t[1], quote=False) if t[0] == "t" else t[1] for t in ln).strip())
        elif cur:
            paras.append("<p>" + "<br>".join(cur) + "</p>"); cur = []
    body = "".join(paras)
    # a tag opened on one line and closed on another must still be balanced
    for el in ("strong", "em", "a"):
        diff = len(re.findall(rf"<{el}[ >]", body)) - body.count(f"</{el}>")
        body += f"</{el}>" * max(diff, 0)
    text = re.sub(r"\s+", " ", " ".join(plain(ln) for ln in rest)).strip()
    return {"id": m["id"], "date": m["date"], "title": title or "Запись из канала", "html": body,
            "plain": (text or title)[:200], "link": f"https://t.me/{channel}/{m['id']}",
            "images": m["images"][:8], "video": m["video"], "source": "channel"}


def read_channel(fx, cfg, known):
    """Return (posts dict, warning or ''). Never drops a known post without direct proof."""
    ch, tag = cfg["channel"], cfg["tag"]
    try:
        main = messages(fx.page(f"s/{ch}"), ch)
    except Exception:
        main = {}
    if not main:
        return known, "канал сейчас не читается. Записи на сайте остались прежними. Новую запись можно добавить в лист «Блог»"
    found = dict(main)
    q = urllib.parse.quote(tag)
    path, seen = f"s/{ch}?q={q}", set()
    for _ in range(15):
        try:
            page = fx.page(path)
        except Exception:
            break
        batch = messages(page, ch)
        if not batch or set(batch) <= seen:
            break
        seen |= set(batch)
        found.update(batch)
        oldest = min(int(i) for i in batch)
        if f"before={oldest}" not in page:
            break
        path = f"s/{ch}?q={q}&before={oldest}"
    posts = {i: make_post(m, tag, ch) for i, m in found.items() if has_tag(m["text"], tag) and m["date"]}
    for pid, old in known.items():
        if pid in posts:
            continue
        if pid in found:              # seen and the tag is gone
            continue
        try:
            page = fx.page(f"{ch}/{pid}?embed=1&mode=tme")
        except Exception:
            posts[pid] = old
            continue
        m = messages(page, ch).get(pid)
        if m and m["text"]:
            if has_tag(m["text"], tag):
                posts[pid] = make_post({**m, "date": m["date"] or old["date"]}, tag, ch)
        elif "Post not found" not in page:
            posts[pid] = old          # unclear answer: keep
    return posts, ""


def save_images(fx, root, post):
    keep = []
    for im in post.get("images", []):
        d = root / "blog" / "img"
        have = sorted(d.glob(f"{post['id']}-{im['key']}.*")) if d.exists() else []
        if not have:
            try:
                if not im.get("url") or not re.match(r"https://[\w.-]*(telesco\.pe|telegram-cdn\.org|cdn-telegram\.org)/", im["url"]):
                    continue
                data = fx.image(im["url"])
            except Exception:
                continue
            ext = "jpg" if data[:3] == b"\xff\xd8\xff" else "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "webp" if data[8:12] == b"WEBP" else None
            if not ext:
                continue
            d.mkdir(parents=True, exist_ok=True)
            have = [d / f"{post['id']}-{im['key']}.{ext}"]
            have[0].write_bytes(data)
        keep.append({"key": im["key"], "file": f"blog/img/{have[0].name}"})
    return keep


# ---------------------------------------------------------------- rendering
def typo(v):
    v = html.escape(v, quote=True)
    v = re.sub(r"(\d) (?=(₽|дн|месяц|сесси|час|лет|год))", r"\1" + NB, v)
    return v.replace(NB, "&nbsp;")


def money(n):
    return f"{n:,}".replace(",", "&nbsp;") + "&nbsp;₽"


def price_line(f, sep):
    parts = []
    for p, l in ((f["p1"], f["l1"]), (f["p2"], f["l2"])):
        if p is not None:
            parts.append((money(p), typo(l)))
    return sep(parts) if parts else ""


def render_formats(prices):
    out = []
    for f in prices:
        line = price_line(f, lambda ps: "<br>".join(m + (f" <small>{l}</small>" if l else "") for m, l in ps))
        tag = f'\n        <span class="tag">{typo(f["tag"])}</span>' if f["tag"] else ""
        out.append(
            f'      <article class="fcard" data-id="{f["code"]}">{tag}\n'
            f'        <h3>{typo(f["title"])}</h3>\n'
            f'        <p class="what"><b>{typo(f["name"])}.</b> {typo(f["desc"])}</p>\n'
            + (f'        <p class="for">{typo(f["for"])}</p>\n' if f["for"] else "")
            + (f'        <p class="price">{line}</p>\n' if line else "")
            + f'        <a class="btn {"btn-ghost" if f["tag"] else "btn-lime"} tg" data-msg="{html.escape(f["msg"], quote=True)}" href="#">{typo(f["button"])}</a>\n'
            f'      </article>')
    return "\n".join(out)


def render_reviews(reviews):
    return "\n".join(
        f'      <div class="rv" data-t="{r["theme"]}"><q>{typo(r["text"])}</q><footer><b>{typo(r["name"])}</b>{typo(r["role"])}'
        + (f'<br>{typo(r["result"])}' if r["result"] else "") + "</footer></div>" for r in reviews)


def ru_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {MONTHS[d.month - 1]}"


def render_cards(posts):
    return "\n".join(
        f'      <a class="post" href="/blog/{p["id"]}/"><time datetime="{p["date"]}">{ru_date(p["date"])}</time><p>{typo(p["title"])}</p><span>Читать →</span></a>'
        for p in posts[:3])


def between(page, name, body):
    pat = re.compile(rf"(<!--sync:{name}-->).*?(<!--/sync:{name}-->)", re.S)
    page, n = pat.subn(lambda m: m.group(1) + "\n" + body + "\n      " + m.group(2), page, count=1)
    if n != 1:
        raise RuntimeError(f"marker {name} missing in template")
    return page


def js_str(v):
    return v.replace("\\", "\\\\").replace("'", "\\'").replace("<", "\\x3c")


AMOUNT = re.compile(r"\d{1,3}(?:&nbsp;\d{3})*&nbsp;₽")


def quiz_line(template_line, f, seed):
    """Keep the hand-written quiz sentence and swap only the amounts. Rebuild it if the captions changed."""
    new = [money(p) for p in (f["p1"], f["p2"]) if p is not None]
    if seed and (f["l1"], f["l2"]) == (seed["l1"], seed["l2"]) and len(AMOUNT.findall(template_line)) == len(new):
        it = iter(new)
        return AMOUNT.sub(lambda m: next(it), template_line)
    return js_str(price_line(f, lambda ps: " или ".join(m + (f" {l}" if l else "") for m, l in ps)))


def render_index(tpl, prices, reviews, posts, seed):
    page = between(tpl, "formats", render_formats(prices))
    page = between(page, "reviews", render_reviews(reviews))
    if posts:
        page = between(page, "posts", render_cards(posts))
        page = page.replace("<!--sync:allposts-->", '<a class="btn btn-line" href="/blog/" style="margin-right:12px">Все записи</a>')
    by = {f["code"]: f for f in prices}
    for code, f in by.items():
        page = re.sub(rf"f:'((?:[^'\\]|\\.)*)',s:'((?:[^'\\]|\\.)*)',id:'{code}'",
                      lambda m: f"f:'{js_str(typo(f['name']))}',s:'{quiz_line(m.group(2), f, seed.get(code))}',id:'{code}'", page)
    d = by.get("diag")
    if d and d["p1"]:
        page = re.sub(r'(<meta name="description" content="[^"]*?за )[\d \xa0]+₽',
                      lambda m: m.group(1) + f"{d['p1']:,}".replace(",", " ") + " ₽", page, count=1)
    return page


def page_shell(tpl, title, desc, canonical, body, extra_head=""):
    style = "".join(re.findall(r"<style>.*?</style>", tpl, re.S)).replace("url(assets/", "url(/assets/")
    css = ("<style>.blog{max-width:760px;margin:0 auto;padding:40px var(--gut) 80px}.blog h1{font-size:clamp(34px,5vw,52px);margin:10px 0 24px}"
           ".blog time{color:var(--mut-light);font-size:15px}.blog p{margin:0 0 18px;overflow-wrap:anywhere}.blog img{border-radius:10px;margin:0 0 18px;width:100%}"
           ".blog .item{display:block;text-decoration:none;border-top:1px solid var(--line-l);padding:22px 0}.blog .item b{display:block;font-family:var(--serif);font-size:28px;font-weight:500;line-height:1.25;margin:6px 0}"
           ".blog .home{display:inline-flex;min-height:44px;align-items:center;font-family:var(--serif);font-size:26px;font-weight:600;text-decoration:none}"
           ".blog .btn{margin-top:12px}.blog .back{display:inline-flex;min-height:44px;align-items:center}</style>")
    e = lambda v: html.escape(v, quote=True)
    return (f'<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f'<title>{e(title)}</title>\n<meta name="description" content="{e(desc)}">\n<link rel="canonical" href="{e(canonical)}">\n'
            '<link rel="icon" href="/favicon.ico" sizes="48x48">\n<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
            f'{extra_head}{style}\n{css}\n</head>\n<body class="light">\n<main class="blog"><a class="home" href="/">Эля Рыбина</a>\n{body}\n</main>\n</body>\n</html>\n')


def render_blog(root, tpl, cfg, posts):
    base, d = cfg["base"], root / "blog"
    d.mkdir(exist_ok=True)
    live = {p["id"] for p in posts}
    for old in d.iterdir():
        if old.is_dir() and old.name != "img" and old.name not in live:
            shutil.rmtree(old)
    used = {im["file"] for p in posts for im in p.get("images", [])}
    if (d / "img").exists():
        for f in (d / "img").iterdir():
            if f"blog/img/{f.name}" not in used:
                f.unlink()
    for p in posts:
        imgs = "".join(f'<img src="/{im["file"]}" alt="" loading="lazy">' for im in p.get("images", []))
        more = f'<p><a class="btn btn-line" href="{p["link"]}" target="_blank" rel="noopener">{"Видео в канале" if p.get("video") else "Открыть в канале"}</a></p>' if p["link"] else ""
        ld = json.dumps({"@context": "https://schema.org", "@type": "BlogPosting", "headline": p["title"], "datePublished": p["date"],
                         "author": {"@type": "Person", "name": "Эля Рыбина", "url": base}, "url": f"{base}blog/{p['id']}/"}, ensure_ascii=False).replace("<", "\\u003c")
        cta = '<p><a class="btn btn-lime" href="https://t.me/ellyrybina" target="_blank" rel="noopener">Написать мне в Telegram</a></p>'
        body = f'<a class="back" href="/blog/">Все записи</a><br><time datetime="{p["date"]}">{ru_date(p["date"])} {p["date"][:4]}</time><h1>{typo(p["title"])}</h1>{imgs}{p["html"]}{more}{cta}'
        (d / p["id"]).mkdir(exist_ok=True)
        (d / p["id"] / "index.html").write_text(page_shell(tpl, f"{p['title']} | Эля Рыбина", p["plain"][:160], f"{base}blog/{p['id']}/", body,
                                                           f'<script type="application/ld+json">{ld}</script>\n'), encoding="utf-8")
    items = "".join(f'<a class="item" href="/blog/{p["id"]}/"><time datetime="{p["date"]}">{ru_date(p["date"])} {p["date"][:4]}</time><b>{typo(p["title"])}</b></a>' for p in posts)
    body = "<h1>Блог</h1>" + (items or "<p>Скоро здесь появятся записи.</p>")
    robots = "" if posts else '<meta name="robots" content="noindex">\n'
    (d / "index.html").write_text(page_shell(tpl, "Блог | Эля Рыбина", "Записи коуча Эли Рыбиной.", base + "blog/", body, robots), encoding="utf-8")


def render_status(tpl, cfg, state, notes):
    def row(name, ok, good, bad):
        return f'<p><b>{"✅" if ok else "⚠️"} {name}.</b> {html.escape(good if ok else bad)}</p>'
    body = (f"<h1>Состояние сайта</h1><p>Последнее изменение сайта: {state['changed_at']} (Москва). Проверка идёт каждые 15 минут.</p>"
            + row("Цены", not notes.get("prices"), f"прочитаны, форматов на сайте: {len(state['prices'])}", f"{notes.get('prices')}. Сайт показывает прежние цены.")
            + row("Отзывы", not notes.get("reviews"), f"прочитаны, отзывов на сайте: {len(state['reviews'])}", f"{notes.get('reviews')}. Сайт показывает прежние отзывы.")
            + row("Канал", not notes.get("channel"), f"читается, записей с {cfg['tag']}: {len(state['channel'])}", f"{notes.get('channel')}.")
            + row("Лист «Блог»", not notes.get("blog"), f"прочитан, записей из листа: {len([m for m in state['manual'] if not m.get('hidden')])}", f"{notes.get('blog')}. Записи из листа остались прежними."))
    return page_shell(tpl, "Состояние сайта", "Служебная страница.", cfg["base"] + "status.html", body, '<meta name="robots" content="noindex">\n')


def visible_posts(state):
    merged = dict(state["channel"])
    for m in state["manual"]:
        if m.get("hidden"):
            merged.pop(m["id"], None)
        else:
            old = merged.get(m["id"], {})
            merged[m["id"]] = {**m, "images": old.get("images", []), "video": old.get("video", False)}
    return sorted(merged.values(), key=lambda p: (p["date"], p["id"]), reverse=True)


def run(root, fx=None, now=None):
    root = Path(root)
    src = Path(__file__).resolve().parent
    if (root / "src").exists():
        src = root / "src"
    cfg = json.loads((src / "config.json").read_text(encoding="utf-8"))
    tpl = (src / "template.html").read_text(encoding="utf-8")
    fx = fx or NetFetcher(cfg)
    now = now or dt.datetime.now(MSK)
    sf = root / "data" / "state.json"
    if sf.exists():
        state = json.loads(sf.read_text(encoding="utf-8"))
    else:
        seed = FileFetcher(src / "seed")
        state = {"prices": parse_prices(seed.sheet("prices"), cfg["core"]), "reviews": parse_reviews(seed.sheet("reviews")),
                 "manual": [], "channel": {}, "changed_at": "", "signature": ""}
    notes = {}
    for key, label, parse in (("prices", "Цены", lambda t: parse_prices(t, cfg["core"])),
                              ("reviews", "Отзывы", parse_reviews),
                              ("blog", "Блог", lambda t: parse_manual(t, cfg["channel"]))):
        try:
            data = parse(fx.sheet(key))
            if key == "blog":
                # a fully cleared sheet is treated as an accident
                if data or not state["manual"]:
                    state["manual"] = data
                else:
                    notes[key] = "лист «Блог» очищен целиком. Чтобы убрать запись, поставьте «нет» в столбце «Показывать»"
            else:
                state[key] = data
        except SheetError as e:
            notes[key] = str(e)
        except Exception:
            notes[key] = f"лист «{label}» не открылся. Проверим снова через час"
    posts, warn = read_channel(fx, cfg, state["channel"])
    if warn:
        notes["channel"] = warn
    for p in posts.values():
        if not warn:
            p["images"] = save_images(fx, root, p)
    state["channel"] = posts

    shown_posts = visible_posts(state)
    signature = hashlib.sha256(json.dumps([state["prices"], state["reviews"], shown_posts, notes, hashlib.sha256(tpl.encode()).hexdigest()],
                                          ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if signature != state["signature"]:
        state["signature"] = signature
        state["changed_at"] = now.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    day = dt.datetime.strptime(state["changed_at"], "%d.%m.%Y %H:%M").date().isoformat()

    seed = {f["code"]: f for f in parse_prices(FileFetcher(src / "seed").sheet("prices"), cfg["core"])}
    (root / "index.html").write_text(render_index(tpl, state["prices"], state["reviews"], shown_posts, seed), encoding="utf-8")
    render_blog(root, tpl, cfg, shown_posts)
    (root / "status.html").write_text(render_status(tpl, cfg, state, notes), encoding="utf-8")
    urls = [(cfg["base"], day)] + ([(cfg["base"] + "blog/", shown_posts[0]["date"])] if shown_posts else []) \
        + [(f"{cfg['base']}blog/{p['id']}/", p["date"]) for p in shown_posts]
    (root / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                      + "".join(f"  <url><loc>{u}</loc><lastmod>{d}</lastmod></url>\n" for u, d in urls) + "</urlset>\n", encoding="utf-8")
    sf.parent.mkdir(exist_ok=True)
    sf.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    # one small commit a month keeps the GitHub schedule from being switched off after 60 idle days
    (root / "data" / "heartbeat.txt").write_text(now.strftime("%Y-%m") + "\n")
    return {"notes": notes, "posts": len(shown_posts), "changed_at": state["changed_at"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--fixtures")
    a = ap.parse_args()
    result = run(a.root, FileFetcher(a.fixtures) if a.fixtures else None)
    print(json.dumps(result, ensure_ascii=False))
