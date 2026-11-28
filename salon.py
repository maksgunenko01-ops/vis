"""Автосалон для Моторса: бот читает Discord-канал «автосалоны-обычные» и отдаёт машины в мини-апп.
Работает и с обычным текстовым каналом (одно сообщение = одна машина), и с форумом (один пост = одна машина).
Подключается в bot.py: setup_salon(web_app, dc, db, auth, reply, CORS, fmt, log_tx, bank_log, GUILD_ID)."""
import re, json, time, asyncio, logging
import discord
from discord import app_commands
from aiohttp import web

log = logging.getLogger("vyshegrad.salon")
CHANNEL_NAME = "автосалоны-обычные"
TTL = 60                      # как часто (сек) бот заново читает канал
LIMIT = 100                   # сколько последних сообщений / постов брать
_cache = {"ts": 0, "items": []}
_lock = asyncio.Lock()

PRICE_KEY = re.compile(r"(?:цена|стоимость|price|купить за|₽|\$|⚛)\D{0,6}?(\d[\d\s.,]*\d|\d)\s*([кk]|тыс)?", re.I)
PRICE_SUF = re.compile(r"(\d[\d\s.,]*\d|\d)\s*([кk]|тыс\.?)?\s*(?:₽|\$|⚛|р\b|руб|влет|вл\b)", re.I)
SHORT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(млн|кк|м\b|к\b|k\b|тыс)", re.I)
ANY_NUM = re.compile(r"\d[\d\s.,]*\d|\d")


def _num(s, suf):
    s = re.sub(r"[\s,]", "", s)
    if s.count(".") == 1 and len(s.split(".")[1]) != 3:
        v = float(s)                       # 1.5к
    else:
        v = float(s.replace(".", ""))      # 1.500.000
    if suf:
        v *= 1000
    return int(v)


def parse_price(text):
    for rx in (PRICE_KEY, PRICE_SUF):
        m = rx.search(text)
        if m:
            try:
                v = _num(m.group(1), m.group(2))
                if 0 < v <= 10**9:
                    return v
            except ValueError:
                pass
    m = SHORT.search(text)
    if m:
        k = m.group(2).lower()
        v = int(float(m.group(1).replace(",", ".")) * (10**6 if k in ("млн", "кк", "м") else 1000))
        if 0 < v <= 10**9:
            return v
    best = 0                               # запасной вариант: самое большое число в тексте
    for m in ANY_NUM.finditer(text):
        try:
            v = _num(m.group(0), "")
            if 1000 <= v <= 10**9:
                best = max(best, v)
        except ValueError:
            pass
    return best


def clean_name(s):
    s = re.sub(r"[*_`~>#|]", "", s or "")
    s = re.sub(r"<a?:\w+:\d+>|<@[!&]?\d+>|https?://\S+", "", s)
    return re.sub(r"\s+", " ", s).strip(" -–—:.")[:60]


def first_image(msg):
    for a in msg.attachments:
        if (a.content_type or "").startswith("image") or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return a.url
    for e in msg.embeds:
        if e.image and e.image.url:
            return e.image.url
        if e.thumbnail and e.thumbnail.url:
            return e.thumbnail.url
    return ""


def msg_text(msg):
    t = msg.content or ""
    for e in msg.embeds:
        t += "\n" + " ".join(x for x in (e.title, e.description) if x)
        t += "\n" + " ".join(f"{f.name} {f.value}" for f in e.fields)
    return t


def make_item(msg, title=None):
    text = msg_text(msg)
    img = first_image(msg)
    first = next((l for l in text.splitlines() if clean_name(l)), "")
    name = clean_name(title) or clean_name(first)
    price = parse_price((title or "") + "\n" + text)
    if not name or price <= 0:
        return None
    note = clean_name(text.replace(first, "", 1))[:200] if title is None else clean_name(text)[:200]
    return {"id": msg.id % 2_000_000_000, "mid": str(msg.id), "name": name, "car": name + (f"|{img}" if img else ""),
            "price": price, "ph": 0, "img": img, "note": note,
            "ts": int(msg.created_at.timestamp()), "sname": "Автосалон"}


def find_channel(guild_id, db):
    row = db.execute("select v from cfg where k='salon_channel'").fetchone()
    from_dc = _holder["dc"]
    if row and row["v"].isdigit():
        ch = from_dc.get_channel(int(row["v"]))
        if ch:
            return ch
    guilds = [g for g in from_dc.guilds if not guild_id or str(g.id) == str(guild_id)]
    for g in guilds:
        for ch in g.channels:
            if ch.name == CHANNEL_NAME:
                return ch
    return None


async def read_channel(ch):
    out = []
    if isinstance(ch, discord.ForumChannel):
        threads = list(ch.threads)
        try:
            async for t in ch.archived_threads(limit=LIMIT):
                threads.append(t)
        except Exception:
            pass
        for t in sorted(threads, key=lambda t: t.id, reverse=True)[:LIMIT]:
            try:
                m = t.starter_message or await t.fetch_message(t.id)
            except Exception:
                continue
            it = make_item(m, title=t.name)
            if it:
                it["id"] = t.id % 2_000_000_000
                it["mid"] = str(t.id)
                out.append(it)
    else:
        async for m in ch.history(limit=LIMIT):
            if m.author.bot and not (m.embeds or m.attachments):
                continue
            it = make_item(m)
            if it:
                out.append(it)
    return out


async def listing(db, guild_id, force=False):
    async with _lock:
        if force or time.time() - _cache["ts"] > TTL:
            ch = find_channel(guild_id, db)
            if not ch:
                raise RuntimeError("salon channel not found")
            _cache["items"] = await read_channel(ch)
            _cache["ts"] = time.time()
        sold = {r["mid"] for r in db.execute("select mid from salon_sold")}
        return [x for x in _cache["items"] if x["mid"] not in sold]


def setup_salon(web_app, dc, db, auth, reply, CORS, fmt, log_tx, bank_log, guild_id):
    _holder["dc"] = dc
    db.execute("create table if not exists salon_sold(mid text primary key, tg_id integer, name text, price integer, ts integer)")
    db.commit()

    @web.middleware
    async def mw(req, handler):
        if req.path.rstrip("/") != "/api/market" or req.method != "POST":
            return await handler(req)
        try:
            body = await req.json()
        except Exception:
            return await handler(req)
        act = body.get("act")
        if act not in ("salon", "sbuy"):
            return await handler(req)          # остальное (рынок между игроками) делает market.py
        p, body, err = await auth(req)
        if err:
            return err
        if act == "salon":
            try:
                items = await listing(db, guild_id)
            except Exception as e:
                log.warning("salon: %s", e)
                return reply({"error": "salon"}, 503)
            return reply({"items": [{k: v for k, v in x.items() if k != "mid"} for x in items]})
        # покупка
        try:
            items = await listing(db, guild_id)
        except Exception:
            return reply({"error": "gone"}, 400)
        it = next((x for x in items if x["id"] == body.get("id")), None)
        if not it:
            return reply({"error": "gone"}, 400)
        price = it["price"]
        p = db.execute("select * from players where tg_id=?", (p["tg_id"],)).fetchone()
        cash, bank = p["cash"] or 0, p["bank"] or 0
        if cash + bank < price:
            return reply({"error": "funds"}, 400)
        from_cash = min(cash, price)
        from_bank = price - from_cash
        cars = json.loads(p["cars"] or "[]")
        cars.append(it["car"])
        try:
            db.execute("insert into salon_sold(mid,tg_id,name,price,ts) values(?,?,?,?,?)",
                       (it["mid"], p["tg_id"], it["name"], price, int(time.time())))
        except Exception:
            return reply({"error": "gone"}, 400)       # уже купили
        db.execute("update players set cash=coalesce(cash,0)-?, bank=coalesce(bank,0)-?, cars=? where tg_id=?",
                   (from_cash, from_bank, json.dumps(cars, ensure_ascii=False), p["tg_id"]))
        db.execute("insert into purchases(tg_id,kind,item,price,ts) values(?,?,?,?,?)",
                   (p["tg_id"], "cars", it["name"], price, int(time.time())))
        db.commit()
        log_tx(p["tg_id"], "salon", -price, f"Автосалон: {it['name']}")
        asyncio.create_task(bank_log(f"🏢 Автосалон: {p['name']} купил(а) «{it['name']}» за {fmt(price)}"))
        n = db.execute("select cash, bank, cars from players where tg_id=?", (p["tg_id"],)).fetchone()
        return reply({"cash": n["cash"], "bank": n["bank"], "cars": json.loads(n["cars"])})

    web_app.middlewares.append(mw)

    @dc.tree.command(name="setsalon", description="Канал автосалона (откуда бот берёт машины для Моторса)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def d_setsalon(i: discord.Interaction, channel: discord.abc.GuildChannel):
        db.execute("insert into cfg(k,v) values('salon_channel',?) on conflict(k) do update set v=excluded.v", (str(channel.id),))
        db.commit()
        _cache["ts"] = 0
        await i.response.send_message(f"Автосалон: беру машины из {channel.mention}", ephemeral=True)

    @dc.tree.command(name="salonrefresh", description="Заново прочитать канал автосалона")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def d_salonrefresh(i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            n = len(await listing(db, guild_id, force=True))
        except Exception as e:
            return await i.followup.send(f"Не вышло: {e}", ephemeral=True)
        await i.followup.send(f"Машин в салоне: {n}", ephemeral=True)


_holder = {}
