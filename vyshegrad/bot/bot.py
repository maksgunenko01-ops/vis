"""Вышеградский бот: Telegram + Discord + маленький API для мини-аппа. Общая база SQLite.
Telegram: открывает мини-апп с данными игрока.
Discord: админские команды (деньги, работа, имущество, Roblox) и история покупок за 30 дней.
API: /api/me, /api/transfer (наличные <-> карта), /api/roblox (привязка ника Roblox)."""
import os, re, json, time, base64, sqlite3, secrets, asyncio, logging, hmac, hashlib
from urllib.parse import parse_qsl
import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
TG_TOKEN = os.environ["TG_TOKEN"]
DC_TOKEN = os.environ["DC_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]                      # https-адрес, где лежит index.html
DISCORD_INVITE = os.getenv("DISCORD_INVITE", "https://discord.gg/PNSYceKEZ")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
ADMIN_TG = os.getenv("ADMIN_TG", "Mle0sk")
API_URL = os.getenv("API_URL", "")                          # публичный https-адрес бота + /api/me
CUR, RATE = "\u269B\uFE0E", 2.5

db = sqlite3.connect(os.getenv("DB_PATH", "vyshegrad.db"))
db.row_factory = sqlite3.Row
db.executescript("""
create table if not exists players(
  tg_id integer primary key, discord_id text unique, name text, username text,
  level integer default 1, money integer default 0, job text default 'Безработный',
  joined text, cars text default '[]', estate text default '[]');
create table if not exists purchases(
  id integer primary key autoincrement, tg_id integer, kind text, item text, price integer, ts integer);
create table if not exists links(code text primary key, tg_id integer, ts integer);
""")
for col, ty in (("cash", "integer default 0"), ("bank", "integer default 0"), ("rbx_id", "integer"),
                ("rbx_name", "text"), ("rbx_dn", "text"), ("rbx_av", "text")):
    try:
        db.execute(f"alter table players add column {col} {ty}")
    except sqlite3.OperationalError:
        pass                                                # колонка уже есть
db.commit()

fmt = lambda n: f"{int(n):,}".replace(",", " ")
month_ago = lambda: int(time.time()) - 30 * 86400
nm = lambda s: str(s).split("|")[0]                         # имя без ссылки на картинку

def ensure(user):
    p = db.execute("select * from players where tg_id=?", (user.id,)).fetchone()
    name = (user.full_name or "Игрок")[:30]
    if not p:
        db.execute("insert into players(tg_id,name,username,joined) values(?,?,?,date('now'))",
                   (user.id, name, user.username or ""))
    else:
        db.execute("update players set name=?, username=? where tg_id=?", (name, user.username or "", user.id))
    db.commit()
    return db.execute("select * from players where tg_id=?", (user.id,)).fetchone()

def fresh(tg_id):
    return db.execute("select * from players where tg_id=?", (tg_id,)).fetchone()

def by_discord(did):
    return db.execute("select * from players where discord_id=?", (str(did),)).fetchone()

def spent(tg_id):
    r = db.execute("select coalesce(sum(price),0) s, count(*) c from purchases where tg_id=? and ts>=?",
                   (tg_id, month_ago())).fetchone()
    return r["s"], r["c"]

def player_dict(p):
    """Полные данные игрока: всё имущество без обрезки."""
    rbx = {"id": p["rbx_id"], "name": p["rbx_name"], "dn": p["rbx_dn"], "av": p["rbx_av"]} if p["rbx_id"] else None
    return {"n": p["name"], "u": p["username"], "id": p["tg_id"], "lv": p["level"], "m": p["money"] or 0,
            "cash": p["cash"] or 0, "bank": p["bank"] or 0, "rbx": rbx,
            "j": p["job"], "s": p["joined"], "c": json.loads(p["cars"]), "e": json.loads(p["estate"]),
            "cur": CUR, "rate": RATE, "adm": ADMIN_TG, "disc": DISCORD_INVITE,
            "cid": DISCORD_CLIENT_ID, "gid": GUILD_ID, "reg": 1 if p["discord_id"] else 0}

def enc(d):
    raw = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def webapp_link(p):
    """Короткая ссылка для кнопки (у Telegram есть лимит на длину). Полные данные приложение
    дозагружает само через /api/me. Без API_URL в ссылку влезает только часть списка."""
    d = player_dict(p)
    if API_URL:
        d["api"] = API_URL
    while len(enc(d)) > 1500 and (d["c"] or d["e"]):
        k = "c" if len(d["c"]) >= len(d["e"]) else "e"
        d[k] = d[k][:-1]
    return f"{WEBAPP_URL}?d={enc(d)}"

# ---------------- Roblox ----------------
async def rbx_lookup(name):
    """Ищет игрока Roblox по нику. Возвращает dict или None, если такого нет."""
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", name or ""):
        return None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as ses:
        async with ses.post("https://users.roblox.com/v1/usernames/users",
                            json={"usernames": [name], "excludeBannedUsers": False}) as r:
            j = await r.json()
        data = j.get("data") or []
        if not data:
            return None
        u, av = data[0], ""
        try:
            async with ses.get("https://thumbnails.roblox.com/v1/users/avatar-headshot",
                               params={"userIds": u["id"], "size": "150x150", "format": "Png", "isCircular": "false"}) as r2:
                t = await r2.json()
            av = ((t.get("data") or [{}])[0]).get("imageUrl") or ""
        except Exception:
            pass
    return {"id": int(u["id"]), "name": u["name"], "dn": u.get("displayName") or u["name"], "av": av}

def save_rbx(tg_id, info):
    db.execute("update players set rbx_id=?, rbx_name=?, rbx_dn=?, rbx_av=? where tg_id=?",
               (info["id"], info["name"], info["dn"], info["av"], tg_id))
    db.commit()

# ---------------- API для мини-аппа ----------------
def check_init(init_data):
    """Проверка подписи Telegram: возвращает id пользователя или None."""
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        h = data.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        key = hmac.new(b"WebAppData", TG_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(data.get("auth_date", "0")) > 86400:
            return None
        return json.loads(data["user"])["id"]
    except Exception:
        return None

CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS"}
reply = lambda obj, status=200: web.json_response(obj, status=status, headers=CORS)

async def auth(req):
    """Возвращает (игрок, тело запроса, None) или (None, None, готовый ответ с ошибкой)."""
    try:
        body = await req.json()
    except Exception:
        return None, None, reply({"error": "bad"}, 400)
    uid = check_init(str(body.get("initData", "")))
    p = fresh(uid) if uid else None
    if not p:
        return None, None, reply({"error": "auth"}, 401)
    return p, body, None

async def api_me(req):
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, _, err = await auth(req)
    return err or reply(player_dict(p))

async def api_transfer(req):
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, body, err = await auth(req)
    if err:
        return err
    try:
        amt = int(body.get("amount"))
    except (TypeError, ValueError):
        amt = 0
    d = body.get("dir")
    if d not in ("c2b", "b2c") or amt < 1 or amt > 10**9:
        return reply({"error": "bad"}, 400)
    src, dst = ("cash", "bank") if d == "c2b" else ("bank", "cash")   # имена колонок только из этого списка
    if (p[src] or 0) < amt:
        return reply({"error": "funds"}, 400)
    db.execute(f"update players set {src}=coalesce({src},0)-?, {dst}=coalesce({dst},0)+? where tg_id=?",
               (amt, amt, p["tg_id"]))
    db.commit()
    return reply(player_dict(fresh(p["tg_id"])))

async def api_roblox(req):
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, body, err = await auth(req)
    if err:
        return err
    if p["rbx_id"]:
        return reply({"error": "locked"}, 409)              # менять ник может только администрация
    try:
        info = await rbx_lookup(str(body.get("username", "")).strip())
    except Exception:
        return reply({"error": "net"}, 502)
    if not info:
        return reply({"error": "notfound"}, 404)
    save_rbx(p["tg_id"], info)
    return reply(player_dict(fresh(p["tg_id"])))

# ---------------- Telegram ----------------
async def tg_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = ensure(update.effective_user)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть помощника", web_app=WebAppInfo(url=webapp_link(p)))]])
    await update.message.reply_text("Данные обновлены. Нажми кнопку, чтобы открыть помощника.", reply_markup=kb)

async def tg_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой ID: {update.effective_user.id}")

async def tg_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ensure(update.effective_user)
    code = secrets.token_hex(3).upper()
    db.execute("insert into links values(?,?,?)", (code, update.effective_user.id, int(time.time())))
    db.commit()
    await update.message.reply_text(f"Код привязки Discord: {code}\nВ Discord на сервере введи /link и этот код. Он действует 15 минут.")

async def tg_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = ensure(update.effective_user)
    rows = db.execute("select * from purchases where tg_id=? and ts>=? order by ts desc limit 20", (p["tg_id"], month_ago())).fetchall()
    if not rows:
        return await update.message.reply_text("За последние 30 дней покупок нет.")
    s, c = spent(p["tg_id"])
    lines = [f"{time.strftime('%d.%m', time.localtime(r['ts']))}  {nm(r['item'])}  {fmt(r['price'])}" for r in rows]
    await update.message.reply_text(f"Покупки за 30 дней: {c}, на {fmt(s)} {CUR}\n" + "\n".join(lines))

# ---------------- Discord ----------------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
    async def setup_hook(self):
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

cut = lambda t: t if len(t) <= 1900 else t[:1900] + "…"
dc = Bot()
tree = dc.tree
ADM = [app_commands.guild_only(), app_commands.default_permissions(administrator=True)]
def admin(f):
    for d in reversed(ADM): f = d(f)
    return f

KINDS = [app_commands.Choice(name="Машина", value="cars"), app_commands.Choice(name="Недвижимость", value="estate")]
WHERE = [app_commands.Choice(name="Влеты", value="money"), app_commands.Choice(name="Наличные $", value="cash"),
         app_commands.Choice(name="Банковская карта $", value="bank")]

async def target(i: discord.Interaction, member: discord.Member):
    if not i.user.guild_permissions.administrator:
        await i.response.send_message("Только для администраторов.", ephemeral=True); return None
    p = by_discord(member.id)
    if not p:
        await i.response.send_message(f"{member.mention} ещё не привязал Telegram (команда /link).", ephemeral=True)
    return p

@tree.command(name="link", description="Привязать Telegram по коду из бота")
async def d_link(i: discord.Interaction, code: str):
    r = db.execute("select * from links where code=?", (code.strip().upper(),)).fetchone()
    if not r or time.time() - r["ts"] > 900:
        return await i.response.send_message("Код неверный или устарел.", ephemeral=True)
    db.execute("update players set discord_id=? where tg_id=?", (str(i.user.id), r["tg_id"]))
    db.execute("delete from links where code=?", (r["code"],)); db.commit()
    await i.response.send_message("Готово, Discord привязан к аккаунту.", ephemeral=True)

@tree.command(name="balance", description="Мой кошелёк")
async def d_balance(i: discord.Interaction):
    p = by_discord(i.user.id)
    if not p: return await i.response.send_message("Сначала привяжи Telegram: /link.", ephemeral=True)
    await i.response.send_message(
        f"Наличные: ${fmt(p['cash'] or 0)}\nКарта: ${fmt(p['bank'] or 0)}\n"
        f"Влеты: {fmt(p['money'] or 0)} {CUR} (≈ ${fmt((p['money'] or 0) * RATE)})", ephemeral=True)

@tree.command(name="give", description="Выдать или снять деньги (минус = снять)")
@app_commands.choices(where=WHERE)
@admin
async def d_give(i: discord.Interaction, member: discord.Member, amount: int, where: app_commands.Choice[str] = None):
    col = where.value if where else "money"
    if col not in ("money", "cash", "bank"): return
    p = await target(i, member)
    if not p: return
    db.execute(f"update players set {col}=coalesce({col},0)+? where tg_id=?", (amount, p["tg_id"])); db.commit()
    now = fresh(p["tg_id"])[col]
    unit = CUR if col == "money" else "$"
    await i.response.send_message(f"{member.mention}: {amount:+d} ({where.name if where else 'Влеты'}). Теперь: {fmt(now)} {unit}")

@tree.command(name="setjob", description="Назначить работу")
@admin
async def d_job(i: discord.Interaction, member: discord.Member, job: str):
    p = await target(i, member)
    if not p: return
    db.execute("update players set job=? where tg_id=?", (job[:40], p["tg_id"])); db.commit()
    await i.response.send_message(f"{member.mention}: работа — {job[:40]}")

@tree.command(name="setlevel", description="Установить уровень")
@admin
async def d_level(i: discord.Interaction, member: discord.Member, level: int):
    p = await target(i, member)
    if not p: return
    db.execute("update players set level=? where tg_id=?", (level, p["tg_id"])); db.commit()
    await i.response.send_message(f"{member.mention}: уровень {level}")

@tree.command(name="roblox", description="Привязать игрока Roblox (меняет только администрация)")
@admin
async def d_roblox(i: discord.Interaction, member: discord.Member, username: str):
    p = await target(i, member)
    if not p: return
    await i.response.defer(ephemeral=True)
    try:
        info = await rbx_lookup(username.strip())
    except Exception:
        return await i.followup.send("Roblox сейчас не отвечает, попробуй позже.")
    if not info:
        return await i.followup.send("Такого игрока в Roblox нет.")
    save_rbx(p["tg_id"], info)
    await i.followup.send(f"{member.mention}: Roblox — {info['name']} (ID {info['id']})")

@tree.command(name="addasset", description="Выдать имущество (цена спишется с баланса влетов). Для машины можно дать ссылку на картинку")
@app_commands.choices(kind=KINDS)
@admin
async def d_add(i: discord.Interaction, member: discord.Member, kind: app_commands.Choice[str], item: str,
                price: int = 0, image: str = ""):
    p = await target(i, member)
    if not p: return
    name = item.replace("|", " ")[:60]
    if image:
        if not (image.startswith("https://") and len(image) <= 150 and "|" not in image):
            return await i.response.send_message("Картинка: нужна ссылка https:// длиной до 150 символов.", ephemeral=True)
        name = f"{name}|{image}"
    lst = json.loads(p[kind.value]); lst.append(name)
    db.execute(f"update players set {kind.value}=?, money=coalesce(money,0)-? where tg_id=?",
               (json.dumps(lst, ensure_ascii=False), price, p["tg_id"]))
    db.execute("insert into purchases(tg_id,kind,item,price,ts) values(?,?,?,?,?)", (p["tg_id"], kind.value, nm(name), price, int(time.time())))
    db.commit()
    await i.response.send_message(f"{member.mention}: добавлено «{nm(name)}», цена {fmt(price)} {CUR}")

@tree.command(name="removeasset", description="Убрать имущество по названию")
@app_commands.choices(kind=KINDS)
@admin
async def d_rm(i: discord.Interaction, member: discord.Member, kind: app_commands.Choice[str], item: str):
    p = await target(i, member)
    if not p: return
    lst = json.loads(p[kind.value])
    hit = next((x for x in lst if nm(x) == item), None)
    if hit is None: return await i.response.send_message("Такого нет в списке.", ephemeral=True)
    lst.remove(hit)
    db.execute(f"update players set {kind.value}=? where tg_id=?", (json.dumps(lst, ensure_ascii=False), p["tg_id"])); db.commit()
    await i.response.send_message(f"{member.mention}: убрано «{item}»")

@tree.command(name="info", description="Кошелёк, работа, имущество и покупки игрока за 30 дней")
@admin
async def d_info(i: discord.Interaction, member: discord.Member):
    p = await target(i, member)
    if not p: return
    s, c = spent(p["tg_id"])
    cars, est = [nm(x) for x in json.loads(p["cars"])], [nm(x) for x in json.loads(p["estate"])]
    rbx = f"{p['rbx_name']} (ID {p['rbx_id']})" if p["rbx_id"] else "не привязан"
    await i.response.send_message(cut(
        f"**{p['name']}** ({member.mention})\nНаличные: ${fmt(p['cash'] or 0)}, карта: ${fmt(p['bank'] or 0)}, "
        f"влеты: {fmt(p['money'] or 0)} {CUR}\nRoblox: {rbx}\nРабота: {p['job']}, уровень {p['level']}\n"
        f"Машины ({len(cars)}): {', '.join(cars) or '—'}\nНедвижимость ({len(est)}): {', '.join(est) or '—'}\n"
        f"За 30 дней: {c} покупок на {fmt(s)} {CUR}"), ephemeral=True)

@tree.command(name="buys", description="Покупки игрока за последние 30 дней")
@admin
async def d_buys(i: discord.Interaction, member: discord.Member):
    p = await target(i, member)
    if not p: return
    rows = db.execute("select * from purchases where tg_id=? and ts>=? order by ts desc limit 25", (p["tg_id"], month_ago())).fetchall()
    text = "\n".join(f"{time.strftime('%d.%m', time.localtime(r['ts']))}  {r['item']}  {fmt(r['price'])}" for r in rows) or "Покупок нет."
    await i.response.send_message(cut(f"Покупки {member.mention} за 30 дней:\n{text}"), ephemeral=True)

# ---------------- запуск ----------------
async def main():
    app = Application.builder().token(TG_TOKEN).build()
    for name, fn in (("start", tg_start), ("myid", tg_myid), ("link", tg_link), ("history", tg_history)):
        app.add_handler(CommandHandler(name, fn))
    web_app = web.Application()
    for path, fn in (("/api/me", api_me), ("/api/transfer", api_transfer), ("/api/roblox", api_roblox)):
        web_app.router.add_route("*", path, fn)
    web_app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
    async with app:
        await app.start()
        await app.updater.start_polling()
        try:
            await dc.start(DC_TOKEN)
        finally:
            await app.updater.stop()
            await app.stop()
            await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
