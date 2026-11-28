"""Вышеградский бот: Telegram + Discord + маленький API для мини-аппа. Общая база SQLite.
Telegram: открывает мини-апп с данными игрока.
Discord: админские команды (деньги, работа, имущество, Roblox) и история покупок за 30 дней.
API: /api/me, /api/transfer (наличные <-> карта), /api/roblox (привязка ника Roblox)."""
import os, re, json, time, base64, sqlite3, secrets, asyncio, logging, hmac, hashlib
from urllib.parse import parse_qsl, quote
import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)      # иначе токен Telegram печатается в логах
log = logging.getLogger("vyshegrad")
TG_TOKEN = os.environ["TG_TOKEN"]
DC_TOKEN = os.environ["DC_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]                      # https-адрес, где лежит index.html
DISCORD_INVITE = os.getenv("DISCORD_INVITE", "https://discord.gg/PNSYceKEZ")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
ADMIN_TG = os.getenv("ADMIN_TG", "Mle0sk")
# Админ-приложение открывается тем, у кого на сервере Discord роль админа или модератора (проверяет бот).
# Необязательно: ID ролей. Если не заданы, бот узнаёт админа и модератора по правам и названию роли («админ», «модератор»).
def _ids(name):
    return {x.strip() for x in os.getenv(name, "").split(",") if x.strip().isdigit()}
ADMIN_ROLE_IDS = _ids("ADMIN_ROLE_IDS")
MOD_ROLE_IDS = _ids("MOD_ROLE_IDS")
# Необязательно: Telegram-ники / числовые ID, которые попадают в админку в обход Discord (по умолчанию пусто).
# ADMIN_IDS ещё нужны для пуша «новая анкета» в личку админам (свой ID: команда /myid).
ADMIN_NAMES = {x.strip().lstrip("@").lower() for x in os.getenv("ADMIN_USERS", "").split(",") if x.strip()}
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
WORKS_PER_LEVEL = max(1, int(os.getenv("WORKS_PER_LEVEL", "5")))   # сколько выплат /work нужно на +1 уровень
STAFF_SKIP = os.getenv("STAFF_SKIP_CHECK", "0") == "1"        # админы и модераторы не проходят проверку Discord/анкету/приём (0 = проходят как игроки, для тестов)
ADMIN_WEBAPP_URL = os.getenv("ADMIN_WEBAPP_URL", "")          # https-адрес, где лежит admin.html
FORM_RESP_URL = os.getenv("FORM_RESP_URL", "")                # ссылка на таблицу ответов анкеты (необязательно)
FORM_SCRIPT_URL = os.getenv("FORM_SCRIPT_URL", "")              # адрес веб-приложения Apps Script (form_answers.gs)
FORM_SCRIPT_TOKEN = os.getenv("FORM_SCRIPT_TOKEN", "")          # ключ из setup() в Apps Script
EXAM_SCRIPT_URL = os.getenv("EXAM_SCRIPT_URL", "")              # адрес веб-приложения Apps Script (exam_answers.gs, форма «Сдача теории»)
EXAM_SCRIPT_TOKEN = os.getenv("EXAM_SCRIPT_TOKEN", "")          # ключ из setup() в exam_answers.gs
PASS_DIR = os.getenv("PASS_DIR", "passports")                 # куда сохраняются фото паспортов
FORM_URL = os.getenv("FORM_URL", "https://docs.google.com/forms/d/e/1FAIpQLScRiasEA9zCXxewr8334-bm9NjKxYjTmJbt0RJ84NmQpbcQew/viewform")
NEWBIE_ROLE_ID = os.getenv("NEWBIE_ROLE_ID", "")             # роль после проверки Discord (необязательно)
CITIZEN_ROLE_ID = os.getenv("CITIZEN_ROLE_ID", "")           # роль после выдачи паспорта (необязательно)
MEMBERS_INTENT = os.getenv("MEMBERS_INTENT", "0") == "1"     # 1 = бот видит вход людей мгновенно (включи Server Members Intent в портале)
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
for col, ty in (("last_work", "integer default 0"), ("cash", "integer default 0"), ("bank", "integer default 0"), ("rbx_id", "integer"),
                ("rbx_name", "text"), ("rbx_dn", "text"), ("rbx_av", "text"),
                ("pp_name", "text"), ("form_ok", "integer default 0"), ("form_ts", "integer default 0"), ("reg_ts", "integer default 0"),
                ("approved", "integer default 0"), ("approved_ts", "integer default 0"),
                ("rejected", "integer default 0"), ("reject_reason", "text"), ("decided_ts", "integer default 0"), ("decided_by", "text"),
                ("banned", "integer default 0"), ("ban_reason", "text"), ("ban_ts", "integer default 0"), ("ban_by", "text"),
                ("pp_data", "text"), ("reg_data", "text"), ("works", "integer default 0"),
                ("appeal_status", "text"), ("appeal_text", "text"), ("appeal_ts", "integer default 0"), ("appeal_note", "text")):
    try:
        db.execute(f"alter table players add column {col} {ty}")
    except sqlite3.OperationalError:
        pass                                                # колонка уже есть
db.executescript("""
create table if not exists jobs(name text primary key, salary integer, cooldown integer default 60, min_level integer default 1);
create table if not exists cfg(k text primary key, v text);
create table if not exists bans(discord_id text primary key, reason text, ts integer, banned_by text);
create table if not exists ban_history(id integer primary key autoincrement, tg_id integer, discord_id text, kind text, reason text, actor text, ts integer);
create table if not exists ban_msgs(tg_id integer, msg_id integer);
create table if not exists tx(id integer primary key autoincrement, tg_id integer, ts integer, kind text, amount integer, note text);
create table if not exists licenses(id integer primary key autoincrement, tg_id integer, number text, fn text, ln text, tg_nick text, car text, cat text, resp_id text, issued_ts integer, expires_ts integer, issued_by text);
create unique index if not exists lic_resp on licenses(resp_id) where resp_id<>'';
""")
try:
    db.execute("alter table licenses add column cat text")
except sqlite3.OperationalError:
    pass
db.commit()

# гражданские работы (из ролей Discord). insert or ignore: уже изменённые через /jobadd не перезаписываются
CIVIL_JOBS = [
    # (название, зарплата, пауза_мин, мин_уровень)
    ("Водитель маршрутки", 1500, 60, 1),
    ("Водитель мусоровоза", 1600, 60, 1),
    ("Водитель снегоуборочной машины", 1600, 60, 1),
    ("Ремонтник дорог", 1700, 60, 1),
    ("Механик", 2000, 60, 2),
    ("Заводчанин", 1800, 60, 1),
    ("Дальнобойщик", 2500, 60, 3),
    ("Диспетчер Дальнобоя", 2800, 60, 4),
    ("Работник ВАЭС", 3000, 60, 5),
    ("Начальник Дальнобойщиков", 3500, 60, 6),
    ("СМИ", 2200, 60, 3),
]
db.executemany("insert or ignore into jobs(name,salary,cooldown,min_level) values(?,?,?,?)", CIVIL_JOBS)
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
            "cid": DISCORD_CLIENT_ID, "gid": GUILD_ID, "reg": 1 if p["discord_id"] else 0,
            "pp": p["pp_name"] or "", "fm": 1 if p["form_ok"] else 0, "ap": 1 if p["approved"] else 0, "rj": 1 if p["rejected"] and not p["approved"] else 0, "form": FORM_URL, "ep": epoch(), "lic": lic_list(p["tg_id"])[:3], **ban_fields(p)}

def enc(d):
    raw = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def webapp_link(p, stf="", base=None):
    """Короткая ссылка для кнопки (у Telegram есть лимит на длину). Полные данные приложение
    дозагружает само через /api/me. Без API_URL в ссылку влезает только часть списка.
    stf (admin / mod): роль в Discord, от неё в приложении появляется плашка АДМИН."""
    d = player_dict(p)
    if API_URL:
        d["api"] = API_URL
    if stf:
        d["stf"] = stf
        if STAFF_SKIP:
            d["reg"] = d["fm"] = d["ap"] = 1
            d["rj"] = 0
    if d.get("bn"):                                         # в короткой ссылке только суть, остальное приложение дозагрузит из /api/me
        d["apx"] = ""; d["bnr"] = d["bnr"][:100]; d["apn"] = d["apn"][:80]; d["bh"] = []
    if base:
        d["home"] = WEBAPP_URL                              # админке: куда вести кнопку «Помощник»
    while len(enc(d)) > 1500 and (d["c"] or d["e"]):
        k = "c" if len(d["c"]) >= len(d["e"]) else "e"
        d[k] = d[k][:-1]
    return f"{base or WEBAPP_URL}?d={enc(d)}"

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

from taxi import setup_taxi, _OPS as taxi_ops_dict
from market import setup_market
from salon import setup_salon
taxi_ops = lambda: taxi_ops_dict

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

async def auth(req, allow_banned=False):
    """Возвращает (игрок, тело запроса, None) или (None, None, готовый ответ с ошибкой)."""
    try:
        body = await req.json()
    except Exception:
        return None, None, reply({"error": "bad"}, 400)
    uid = check_init(str(body.get("initData", "")))
    p = fresh(uid) if uid else None
    if not p:
        return None, None, reply({"error": "auth"}, 401)
    if p["banned"] and not allow_banned:
        return None, None, reply({"error": "banned"}, 403)
    return p, body, None

def ach_counts():
    """Сколько игроков получили каждое достижение (ключи совпадают с приложением)."""
    c = {"welcome": 0, "cap": 0, "mil": 0, "car": 0, "house": 0, "vet": 0}
    today = time.strftime("%Y-%m-%d")
    for r in db.execute("select cars, estate, money, cash, bank, joined, approved from players where coalesce(banned,0)=0"):
        if r["approved"]: c["welcome"] += 1
        tot = (r["cash"] or 0) + (r["bank"] or 0) + (r["money"] or 0) * RATE
        if tot >= 1e5: c["cap"] += 1
        if tot >= 1e6: c["mil"] += 1
        if (r["cars"] or "[]") not in ("[]", ""): c["car"] += 1
        if (r["estate"] or "[]") not in ("[]", ""): c["house"] += 1
        try:
            if r["joined"] and (time.mktime(time.strptime(today, "%Y-%m-%d")) - time.mktime(time.strptime(r["joined"][:10], "%Y-%m-%d"))) >= 30 * 86400: c["vet"] += 1
        except Exception:
            pass
    return c

async def full(p):
    """Данные игрока + его роль на сервере Discord (stf: admin / mod / пусто)."""
    d = player_dict(p)
    d["ac"] = ach_counts()
    for k, col in (("ppd", "pp_data"), ("rgd", "reg_data")):    # паспорт и карточка Discord: восстанавливаются на любом устройстве
        try:
            v = json.loads(p[col]) if p[col] else None
            if isinstance(v, dict):
                d[k] = v
        except Exception:
            pass
    d["stf"] = "" if p["banned"] else await staff_level(p)   # забаненный не может быть в админке
    if d["stf"] and STAFF_SKIP:
        d["reg"] = d["fm"] = d["ap"] = 1
        d["rj"] = 0
    return d

async def api_me(req):
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, _, err = await auth(req, True)
    return err or reply(await full(p))

async def api_sync(req):
    """Приложение сохраняет на сервере паспорт (k=pass) и карточку Discord (k=reg), чтобы они были на любом устройстве аккаунта."""
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, body, err = await auth(req)
    if err:
        return err
    k, data = body.get("k"), body.get("data")
    col, lim = {"pass": ("pp_data", 400_000), "reg": ("reg_data", 20_000)}.get(k, (None, 0))
    if not col or not isinstance(data, dict):
        return reply({"error": "bad"}, 400)
    txt = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(txt) > lim:
        return reply({"error": "big"}, 413)
    db.execute(f"update players set {col}=? where tg_id=?", (txt, p["tg_id"])); db.commit()
    return reply({"ok": 1})

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
    return reply(await full(fresh(p["tg_id"])))

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
    return reply(await full(fresh(p["tg_id"])))

_pp_last = {}   # tg_id -> время последней отправки паспорта (защита от спама)

async def api_passport(req):
    """Апп присылает готовую картинку паспорта (PNG, data URL), бот отправляет её игроку в Telegram."""
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, body, err = await auth(req)
    if err:
        return err
    nm = clean_nick(body.get("name", ""))
    if nm and nm != (p["pp_name"] or ""):
        db.execute("update players set pp_name=? where tg_id=?", (nm, p["tg_id"])); db.commit()
        if p["discord_id"]:
            asyncio.create_task(rename_member(p["discord_id"], nm))
    img = str(body.get("image", ""))
    pre = "data:image/png;base64,"
    if not img.startswith(pre) or len(img) > 4_000_000:
        return reply({"error": "bad"}, 400)
    now = time.time()
    if now - _pp_last.get(p["tg_id"], 0) < 15:
        return reply({"error": "wait"}, 429)
    try:
        raw = base64.b64decode(img[len(pre):], validate=True)
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError
    except Exception:
        return reply({"error": "bad"}, 400)
    _pp_last[p["tg_id"]] = now
    try:
        os.makedirs(PASS_DIR, exist_ok=True)
        with open(os.path.join(PASS_DIR, f"{p['tg_id']}.png"), "wb") as f:
            f.write(raw)
    except Exception:
        log.exception("passport save failed")
    try:
        await req.app["tg"].bot.send_photo(chat_id=p["tg_id"], photo=raw,
                                           caption="Твой паспорт жителя Вышеграда")
    except Exception:
        log.exception("passport send failed")
        return reply({"error": "tg"}, 502)
    return reply({"ok": 1})


# ---------------- путь жителя: Discord → проверка ботом → паспорт → анкета ----------------
def clean_nick(s):
    s = re.sub(r"[^\w .'\-]", "", str(s or ""), flags=re.U)
    return re.sub(r"\s+", " ", s).strip()[:32]

async def member_of(did):
    """Участник сервера по Discord ID или None. Работает и без Members Intent (через REST)."""
    if not GUILD_ID:
        return None
    g = dc.get_guild(int(GUILD_ID))
    if not g:
        return None
    try:
        return g.get_member(int(did)) or await g.fetch_member(int(did))
    except (discord.NotFound, discord.HTTPException, ValueError):
        return None

async def apply_member(member, nick, role_ids=()):
    """Ставит имя на сервере и выдаёт роли. Ошибки прав не роняют бота."""
    done, want = None, clean_nick(nick)
    if want:
        try:
            if member.nick != want:
                await member.edit(nick=want, reason="Вышеград РП: регистрация")
            done = want
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.warning("nick %s: %s (роль бота должна стоять выше роли игрока)", member.id, e)
    for rid in role_ids:
        role = member.guild.get_role(int(rid)) if str(rid).isdigit() else None
        if role and role not in member.roles:
            try:
                await member.add_roles(role, reason="Вышеград РП: регистрация")
            except (discord.Forbidden, discord.HTTPException) as e:
                logging.warning("role %s: %s", rid, e)
    return done

async def rename_member(did, nick):
    m = await member_of(did)
    if m:
        await apply_member(m, nick, [CITIZEN_ROLE_ID])

async def notify_admins(text):
    """Пишет админам в канал проверки (/setreview), иначе в канал банка."""
    cid = cfg("review_channel", "") or cfg("bank_channel", os.getenv("BANK_CHANNEL_ID", ""))
    if not cid:
        return
    try:
        ch = dc.get_channel(int(cid)) or await dc.fetch_channel(int(cid))
        await ch.send(cut(text))
    except Exception as e:
        logging.warning("notify_admins: %s", e)

_hold = {}                                                  # сюда кладём Telegram-приложение, чтобы писать из команд Discord
WELCOME_IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.png")

async def send_welcome(p):
    """Пишет игроку в Telegram, что анкету проверили, и шлёт картинку «Добро пожаловать в наш Проект!»."""
    tg = _hold.get("tg")
    if not tg:
        return False
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть помощника", web_app=WebAppInfo(url=webapp_link(p)))]])
    cap = ("Добро пожаловать в наш Проект! 🎉\n\n"
           "Админы проверили твою анкету и приняли тебя в Вышеград РП. Полный доступ открыт: работа, кошелёк, имущество.")
    try:
        if os.path.exists(WELCOME_IMG):
            with open(WELCOME_IMG, "rb") as f:
                await tg.bot.send_photo(chat_id=p["tg_id"], photo=f, caption=cap, reply_markup=kb)
        else:
            await tg.bot.send_message(chat_id=p["tg_id"], text=cap, reply_markup=kb)
        return True
    except Exception:
        log.exception("welcome send failed")
        return False

_code_last = {}

async def api_linkcode(req):
    """Код для запасной привязки: в Discord ввести /link КОД."""
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, _, err = await auth(req)
    if err:
        return err
    if time.time() - _code_last.get(p["tg_id"], 0) < 5:
        return reply({"error": "wait"}, 429)
    _code_last[p["tg_id"]] = time.time()
    code = secrets.token_hex(3).upper()
    db.execute("delete from links where tg_id=?", (p["tg_id"],))
    db.execute("insert into links values(?,?,?)", (code, p["tg_id"], int(time.time())))
    db.commit()
    return reply({"code": code, "ttl": 900})

async def api_discord(req):
    """Проверка через вход в Discord: бот сам проверяет токен, членство на сервере и ставит имя."""
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, body, err = await auth(req)
    if err:
        return err
    if not GUILD_ID:
        return reply({"error": "noguild"}, 503)
    tok = str(body.get("token", ""))[:300]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as ses:
            async with ses.get("https://discord.com/api/v10/users/@me", headers={"Authorization": "Bearer " + tok}) as r:
                if r.status != 200:
                    return reply({"error": "token"}, 401)
                u = await r.json()
    except Exception:
        return reply({"error": "net"}, 502)
    did = str(u.get("id", ""))
    bn = db.execute("select * from bans where discord_id=?", (did,)).fetchone()
    if bn and not db.execute("select 1 from players where discord_id=? and tg_id!=?", (did, p["tg_id"])).fetchone():
        db.execute("update players set discord_id=? where tg_id=?", (did, p["tg_id"])); db.commit()
        await do_ban(did, bn["reason"], bn["banned_by"])
        return reply({"error": "banned"}, 403)
    m = await member_of(did)
    if not m:
        return reply({"error": "notmember"}, 404)
    taken = db.execute("select tg_id from players where discord_id=? and tg_id!=?", (did, p["tg_id"])).fetchone()
    if taken:
        return reply({"error": "taken"}, 409)
    first = not p["discord_id"]
    db.execute("update players set discord_id=?, reg_ts=? where tg_id=?", (did, int(time.time()), p["tg_id"])); db.commit()
    nick = await apply_member(m, p["pp_name"] or p["name"] or u.get("username"), [NEWBIE_ROLE_ID, CITIZEN_ROLE_ID if p["pp_name"] else ""])
    if first:
        await notify_admins(f"✅ {m.mention} прошёл(ла) проверку Discord (Telegram: @{p['username'] or p['tg_id']}). Имя на сервере: {nick or '—'}")
    return reply({"ok": 1, "did": did, "dc": u.get("username", ""), "nick": nick or ""})

# ---- кто админ или модератор: решает только Discord ----
# Плашка АДМИН в приложении лишь подсказка. Каждый запрос /api/admin/* бот проверяет заново:
# подпись Telegram -> привязанный Discord -> роль на сервере.
_staff_cache = {}   # tg_id -> (время, уровень)

def _member_level(m):
    """'admin' / 'mod' / '' для участника сервера Discord."""
    if m is None:
        return ""
    if m.guild.owner_id == m.id or m.guild_permissions.administrator:
        return "admin"
    rids = {str(r.id) for r in m.roles}
    if ADMIN_ROLE_IDS or MOD_ROLE_IDS:                      # ID ролей заданы: смотрим строго по ним
        return "admin" if rids & ADMIN_ROLE_IDS else "mod" if rids & MOD_ROLE_IDS else ""
    names = " ".join(r.name.lower() for r in m.roles)       # иначе: по названию роли и правам модератора
    if "админ" in names or "admin" in names:
        return "admin"
    pr = m.guild_permissions
    if "модер" in names or "moder" in names or pr.manage_messages or pr.kick_members or pr.ban_members or pr.moderate_members:
        return "mod"
    return ""

async def staff_level(p):
    if not p:
        return ""
    if p["tg_id"] in ADMIN_IDS or (p["username"] or "").lower() in ADMIN_NAMES:
        return "admin"
    if not p["discord_id"]:
        return ""
    hit = _staff_cache.get(p["tg_id"])
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    lvl = _member_level(await member_of(p["discord_id"]))
    _staff_cache[p["tg_id"]] = (time.time(), lvl)
    return lvl

def admin_url():
    """Где лежит admin.html: ADMIN_WEBAPP_URL, а если не задан, рядом с index.html."""
    if ADMIN_WEBAPP_URL:
        return ADMIN_WEBAPP_URL
    u = WEBAPP_URL.split("?")[0].split("#")[0]
    return (u.rsplit("/", 1)[0] if u.rsplit("/", 1)[-1].endswith(".html") else u.rstrip("/")) + "/admin.html"

def admin_link(p=None, stf="admin"):
    """Ссылка на админку. С игроком: те же данные, что в помощнике, чтобы кнопка «назад» вела в его кабинет."""
    if p is not None:
        return webapp_link(p, stf, admin_url())
    return f"{admin_url()}?api={quote(API_URL, safe='')}&home={quote(WEBAPP_URL.split('?')[0], safe='')}" if API_URL else ""

async def ping_admins_tg(p):
    """Пуш админам в Telegram о новой анкете (нужны ADMIN_IDS: числовые ID, и чтобы админ нажал /start у бота)."""
    tg = _hold.get("tg")
    if not tg or not ADMIN_IDS:
        return
    txt = f"📝 Новая анкета: {p['pp_name'] or p['name']} (@{p['username'] or p['tg_id']}). Фото паспорта и ссылка на анкету в админке."
    for aid in ADMIN_IDS:
        ap = fresh(aid)
        link = admin_link(ap) if ap else admin_link()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть админку", web_app=WebAppInfo(url=link))]]) if link else None
        try:
            await tg.bot.send_message(chat_id=aid, text=txt, reply_markup=kb)
        except Exception:
            log.warning("admin ping failed for %s", aid)

async def send_reject(p, reason):
    tg = _hold.get("tg")
    if not tg:
        return False
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть помощника", web_app=WebAppInfo(url=webapp_link(p)))]])
    txt = "Твою анкету пока не приняли." + (f"\n\nПричина: {reason}" if reason else "") + "\n\nЗаполни анкету заново в приложении и отправь её ещё раз."
    try:
        await tg.bot.send_message(chat_id=p["tg_id"], text=txt, reply_markup=kb)
        return True
    except Exception:
        log.exception("reject send failed")
        return False

def admin_card(p):
    st = "ok" if p["approved"] else ("no" if p["rejected"] else "new")
    return {"id": p["tg_id"], "n": p["pp_name"] or p["name"] or "", "u": p["username"] or "", "ts": p["form_ts"] or 0,
            "dc": 1 if p["discord_id"] else 0, "rbx": p["rbx_name"] or "", "lv": p["level"], "st": st, "dts": p["decided_ts"] or 0,
            "pp": 1 if os.path.exists(os.path.join(PASS_DIR, f"{p['tg_id']}.png")) else 0}

async def admin_auth(req):
    p, body, err = await auth(req)
    if err:
        return None, None, err
    if not await staff_level(p):
        return None, None, reply({"error": "forbidden"}, 403)
    return p, body, None

async def api_admin_list(req):
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    _, _, err = await admin_auth(req)
    if err:
        return err
    rows = db.execute("select * from players where form_ok=1 or approved=1 or rejected=1").fetchall()
    items = [admin_card(r) for r in rows]
    items.sort(key=lambda c: (c["st"] != "new", c["ts"] if c["st"] == "new" else -c["dts"]))
    return reply({"items": items, "form": FORM_URL, "resp": FORM_RESP_URL})

async def api_admin_get(req):
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    _, body, err = await admin_auth(req)
    if err:
        return err
    try:
        t = fresh(int(body.get("id")))
    except Exception:
        t = None
    if not t:
        return reply({"error": "none"}, 404)
    d = admin_card(t)
    d.update({"disc": t["discord_id"] or "", "joined": t["joined"] or "", "reg": t["reg_ts"] or 0, "reason": t["reject_reason"] or "",
              "rbx_dn": t["rbx_dn"] or "", "form": FORM_URL, "resp": FORM_RESP_URL, "img": ""})
    path = os.path.join(PASS_DIR, f"{t['tg_id']}.png")
    if os.path.exists(path):
        with open(path, "rb") as f:
            d["img"] = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    return reply(d)

async def api_admin_decide(req):
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    a, body, err = await admin_auth(req)
    if err:
        return err
    try:
        t = fresh(int(body.get("id")))
    except Exception:
        t = None
    act = body.get("action")
    if not t or act not in ("approve", "reject"):
        return reply({"error": "bad"}, 400)
    if t["tg_id"] == a["tg_id"]:
        return reply({"error": "self"}, 403)                 # свою анкету проверяет другой админ
    now = int(time.time())
    who = f"<@{t['discord_id']}>" if t["discord_id"] else f"@{t['username'] or t['tg_id']}"
    adm = a["username"] or a["tg_id"]
    if act == "approve":
        cur = db.execute("update players set approved=1, approved_ts=?, rejected=0, reject_reason='', decided_ts=?, decided_by=? where tg_id=? and approved=0",
                         (now, now, str(adm), t["tg_id"])); db.commit()
        if not cur.rowcount:                                # уже принята другим админом: второе приветствие не шлём
            return reply({"error": "decided", "st": "ok"}, 409)
        sent = await send_welcome(fresh(t["tg_id"]))
        await notify_admins(f"✅ Анкета принята: {who}. Проверил(а): @{adm}.")
    else:
        reason = str(body.get("reason", "")).strip()[:300]
        cur = db.execute("update players set rejected=1, approved=0, form_ok=0, reject_reason=?, decided_ts=?, decided_by=? where tg_id=? and approved=0",
                         (reason, now, str(adm), t["tg_id"])); db.commit()
        if not cur.rowcount:                                # принятую анкету отклонить нельзя
            return reply({"error": "decided", "st": "ok"}, 409)
        sent = await send_reject(fresh(t["tg_id"]), reason)
        await notify_admins(f"❌ Анкета отклонена: {who}. Проверил(а): @{adm}." + (f" Причина: {reason}" if reason else ""))
    return reply({"ok": 1, "sent": bool(sent)})

_ans_cache = {}   # tg_id -> (время, ответ скрипта)

async def api_admin_answers(req):
    """Ответы игрока из Google Формы: бот спрашивает их у скрипта form_answers.gs."""
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    _, body, err = await admin_auth(req)
    if err:
        return err
    if not (FORM_SCRIPT_URL and FORM_SCRIPT_TOKEN):
        return reply({"error": "noscript"}, 503)
    try:
        t = fresh(int(body.get("id")))
    except Exception:
        t = None
    if not t:
        return reply({"error": "none"}, 404)
    hit = _ans_cache.get(t["tg_id"])
    if hit and time.time() - hit[0] < 60:
        return reply(hit[1])
    params = {"token": FORM_SCRIPT_TOKEN, "u": t["username"] or "", "id": str(t["tg_id"]),
              "n": t["pp_name"] or t["name"] or ""}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as ses:
            async with ses.get(FORM_SCRIPT_URL, params=params, allow_redirects=True) as r:
                j = await r.json(content_type=None)
    except Exception:
        log.exception("form script failed")
        return reply({"error": "net"}, 502)
    if not j.get("ok"):
        return reply({"error": "script"}, 502)
    out = {"found": bool(j.get("found")), "by": j.get("by", ""), "ts": j.get("ts", 0), "total": j.get("total", 0),
           "answers": [{"q": str(x.get("q", ""))[:300], "a": str(x.get("a", ""))[:1500]} for x in (j.get("answers") or [])[:60]]}
    _ans_cache[t["tg_id"]] = (time.time(), out)
    return reply(out)

async def api_form(req):
    """Игрок подтвердил, что заполнил анкету. Ответы лежат в Google Таблице, админы сверяют их позже."""
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, _, err = await auth(req)
    if err:
        return err
    if not p["form_ok"]:
        db.execute("update players set form_ok=1, form_ts=?, rejected=0, reject_reason='' where tg_id=?", (int(time.time()), p["tg_id"])); db.commit()
        who = f"<@{p['discord_id']}>" if p["discord_id"] else f"@{p['username'] or p['tg_id']}"
        await notify_admins(f"📝 Анкета РП-теста: {who} · паспорт: {p['pp_name'] or 'нет'} · TG @{p['username'] or p['tg_id']}. Ждёт проверки админами (админ-приложение или /approve в Discord).")
        asyncio.create_task(ping_admins_tg(fresh(p["tg_id"])))
    return reply({"ok": 1})

# ---------------- Права: форма «Сдача теории» ----------------
LIC_DAYS = 3650                                             # срок действия прав: 10 лет
_exam_cache = {"t": 0, "data": None}

def lic_dict(r):
    return {"id": r["id"], "no": r["number"], "fn": r["fn"], "ln": r["ln"], "tgn": r["tg_nick"] or "", "car": r["car"], "cat": r["cat"] or "",
            "ts": r["issued_ts"], "exp": r["expires_ts"], "by": r["issued_by"] or ""}

def lic_list(tg_id):
    rows = db.execute("select * from licenses where tg_id=? order by id desc", (tg_id,)).fetchall()
    return [lic_dict(r) for r in rows]

def _clean(s, n):
    return " ".join(str(s or "").split())[:n]

def _nick(s):
    s = _clean(s, 80)
    for pre in ("https://t.me/", "http://t.me/", "t.me/"):
        if s.lower().startswith(pre):
            s = s[len(pre):]
    return s.lstrip("@").strip()

def _player_by_nick(nick):
    nick = _nick(nick)
    if not nick:
        return None
    if nick.isdigit():
        return fresh(int(nick))
    return db.execute("select * from players where lower(username)=?", (nick.lower(),)).fetchone()

async def exam_fetch(force=False):
    if not (EXAM_SCRIPT_URL and EXAM_SCRIPT_TOKEN):
        return None, "noscript"
    if not force and _exam_cache["data"] is not None and time.time() - _exam_cache["t"] < 20:
        return _exam_cache["data"], ""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as ses:
            async with ses.get(EXAM_SCRIPT_URL, params={"token": EXAM_SCRIPT_TOKEN}, allow_redirects=True) as r:
                j = await r.json(content_type=None)
    except Exception:
        log.exception("exam script failed")
        return None, "net"
    if not j.get("ok"):
        return None, "script"
    _exam_cache["t"], _exam_cache["data"] = time.time(), j
    return j, ""

async def api_admin_exam(req):
    """Все ответы формы «Сдача теории» для вкладки «Права» в админке."""
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    _, body, err = await admin_auth(req)
    if err:
        return err
    j, e = await exam_fetch(force=bool(body.get("force")))
    if e:
        return reply({"error": e}, 503 if e == "noscript" else 502)
    issued = {r["resp_id"]: r for r in db.execute("select * from licenses where resp_id<>''").fetchall()}
    items = []
    for x in (j.get("items") or [])[:150]:
        rid = _clean(x.get("rid"), 80)
        name = _clean(x.get("name"), 80)
        parts = name.split(" ")
        tgn = _nick(x.get("tg"))
        pl = _player_by_nick(tgn)
        lr = issued.get(rid)
        items.append({
            "rid": rid, "ts": x.get("ts") or 0, "email": _clean(x.get("email"), 120),
            "name": name, "fn": parts[0] if name else "", "ln": " ".join(parts[1:]) if name else "",
            "tg": tgn, "car": _clean(x.get("car"), 60), "cat": _clean(x.get("cat"), 20), "sc": x.get("sc"), "mx": x.get("mx"),
            "pl": {"id": pl["tg_id"], "n": pl["name"] or ""} if pl else None,
            "lic": lic_dict(lr) if lr else None,
            "answers": [{"q": _clean(a.get("q"), 300), "a": str(a.get("a", ""))[:1500],
                         "ok": a.get("ok"), "sc": a.get("sc"), "pts": a.get("pts")} for a in (x.get("answers") or [])[:80]],
        })
    return reply({"items": items, "total": j.get("total", len(items)), "quiz": bool(j.get("quiz"))})

async def api_admin_license(req):
    """Выдать права по ответу формы (action=issue) или отозвать их (action=revoke)."""
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, body, err = await admin_auth(req)
    if err:
        return err
    adm = p["username"] or p["name"] or str(p["tg_id"])
    if (body.get("action") or "issue") == "revoke":
        try:
            r = db.execute("select * from licenses where id=?", (int(body.get("lid")),)).fetchone()
        except Exception:
            return reply({"error": "bad"}, 400)
        if not r:
            return reply({"error": "none"}, 404)
        db.execute("delete from licenses where id=?", (r["id"],)); db.commit()
        sent = await tg_dm(r["tg_id"], f"🪪 Ваши водительские права на «{r['car']}» (№{r['number']}) аннулированы администрацией.")
        return reply({"ok": 1, "sent": bool(sent)})

    fn, ln, car = _clean(body.get("fn"), 40), _clean(body.get("ln"), 40), _clean(body.get("car"), 60)
    cat = _clean(body.get("cat"), 20)
    rid = _clean(body.get("rid"), 80)
    if not fn or not car:
        return reply({"error": "bad"}, 400)
    t = _player_by_nick(body.get("tg"))
    if not t:
        return reply({"error": "noplayer"}, 404)
    if rid and db.execute("select 1 from licenses where resp_id=?", (rid,)).fetchone():
        return reply({"error": "issued"}, 409)
    now = int(time.time())
    cur = db.execute("insert into licenses(tg_id,number,fn,ln,tg_nick,car,cat,resp_id,issued_ts,expires_ts,issued_by) values(?,?,?,?,?,?,?,?,?,?,?)",
                     (t["tg_id"], "", fn, ln, t["username"] or "", car, cat, rid, now, now + LIC_DAYS * 86400, adm))
    lid = cur.lastrowid
    db.execute("update licenses set number=? where id=?", (f"VG{lid:06d}", lid)); db.commit()
    row = db.execute("select * from licenses where id=?", (lid,)).fetchone()
    sent = await tg_dm(t["tg_id"], f"🪪 Поздравляем, {fn}! Вы сдали теорию, вам выданы водительские права.\n"
                                   f"Машина: {car}\nНомер: {row['number']}\n"
                                   "Карточка прав лежит в приложении: раздел «Имущество».", p=fresh(t["tg_id"]))
    try:
        await notify_admins(f"🪪 Права выданы: {fn} {ln} · {car} · №{row['number']}. Выдал(а): @{adm}.")
    except Exception:
        log.exception("notify_admins licence")
    return reply({"ok": 1, "sent": bool(sent), "lic": lic_dict(row)})

# ---------------- Telegram ----------------
async def tg_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = ensure(update.effective_user)
    if p["banned"]:
        return await ban_reply(update, ban_text(p), InlineKeyboardMarkup([[InlineKeyboardButton("Открыть приложение", web_app=WebAppInfo(url=webapp_link(p)))]]))
    lvl = await staff_level(p)
    rows = [[InlineKeyboardButton("Открыть помощника", web_app=WebAppInfo(url=webapp_link(p, lvl)))]]
    if lvl:
        rows.append([InlineKeyboardButton("АДМИН · паспорта и анкеты", web_app=WebAppInfo(url=admin_link(p, lvl)))])
    kb = InlineKeyboardMarkup(rows)
    await update.message.reply_text(("Добро пожаловать в Вышеград РП! Нажми кнопку: Discord, проверка ботом, паспорт и анкета. Всё в одном приложении." if not p["discord_id"] else "Данные обновлены. Нажми кнопку, чтобы открыть помощника."), reply_markup=kb)

async def tg_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = ensure(update.effective_user)
    lvl = await staff_level(p)
    if not lvl:
        return await update.message.reply_text("Админ-панель только для админов и модераторов сервера Discord. Если ты админ, сначала пройди проверку Discord в помощнике (/start).")
    link = admin_link(p, lvl)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть админку", web_app=WebAppInfo(url=link))]])
    await update.message.reply_text("Админ-приложение Вышеграда: анкеты, паспорта, решения.", reply_markup=kb)

async def tg_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой ID: {update.effective_user.id}")

async def tg_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ensure(update.effective_user)["banned"]:
        return await ban_reply(update, "🚫 Аккаунт заблокирован. Открой /start, чтобы узнать статус и обжаловать решение.")
    code = secrets.token_hex(3).upper()
    db.execute("insert into links values(?,?,?)", (code, update.effective_user.id, int(time.time())))
    db.commit()
    await update.message.reply_text(f"Код привязки Discord: {code}\nВ Discord на сервере введи /link и этот код. Он действует 15 минут.")

async def tg_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = ensure(update.effective_user)
    if p["banned"]:
        return await ban_reply(update, "🚫 Аккаунт заблокирован. Открой /start, чтобы узнать статус и обжаловать решение.")
    rows = db.execute("select * from purchases where tg_id=? and ts>=? order by ts desc limit 20", (p["tg_id"], month_ago())).fetchall()
    if not rows:
        return await update.message.reply_text("За последние 30 дней покупок нет.")
    s, c = spent(p["tg_id"])
    lines = [f"{time.strftime('%d.%m', time.localtime(r['ts']))}  {nm(r['item'])}  {fmt(r['price'])}" for r in rows]
    await update.message.reply_text(f"Покупки за 30 дней: {c}, на {fmt(s)} {CUR}\n" + "\n".join(lines))



def _is_adm(u):
    return u.id in ADMIN_IDS or (u.username or "").lower() in ADMIN_NAMES

async def _taxi_cmd(update, ctx, pay):
    if not _is_adm(update.effective_user):
        return
    try:
        oid = int(ctx.args[0])
    except (IndexError, ValueError):
        return await update.message.reply_text("Формат: /taxipay НОМЕР или /taxirefund НОМЕР")
    await update.message.reply_text(taxi_ops()["resolve"](oid, pay))

async def tg_taxipay(update, ctx):
    await _taxi_cmd(update, ctx, True)

async def tg_taxirefund(update, ctx):
    await _taxi_cmd(update, ctx, False)

# ---------------- баны: Discord -> приложение ----------------
BAN_TEXT = "К сожалению, вы депортированы из страны за нарушение закона!"
APPEAL_COOLDOWN = int(os.getenv("APPEAL_COOLDOWN_H", "24")) * 3600      # через сколько часов после отказа можно подать апелляцию снова

def ban_fields(p):
    """Что приложение узнаёт о бане: экран «депортирован», причина, статус апелляции."""
    if not p["banned"]:
        return {"bn": 0}
    return {"bn": 1, "bh": hist_list(p["tg_id"], 6), "bnr": (p["ban_reason"] or "")[:200], "bnt": p["ban_ts"] or 0, "bnb": (p["ban_by"] or "")[:40],
            "ast": p["appeal_status"] or "", "apt": p["appeal_ts"] or 0, "apn": (p["appeal_note"] or "")[:200],
            "apx": (p["appeal_text"] or "")[:300], "case": f"B-{p['tg_id'] % 1000000:06d}", "acd": APPEAL_COOLDOWN}

def ban_text(p):
    r = (p["ban_reason"] or "").strip()
    return "🚫 " + BAN_TEXT + (f"\n\nПричина: {r}" if r else "") + "\n\nОткрой приложение: там можно узнать статус аккаунта и обжаловать решение."

HIST_TITLES = {"ban": "Депортация", "unban": "Депортация отменена", "appeal": "Апелляция подана",
               "appeal_no": "Апелляция отклонена", "appeal_ok": "Апелляция одобрена"}

def hist_add(p, kind, reason="", actor=""):
    """Запись в историю банов игрока (бан, разбан, апелляции)."""
    db.execute("insert into ban_history(tg_id,discord_id,kind,reason,actor,ts) values(?,?,?,?,?,?)",
               (p["tg_id"], p["discord_id"], kind, (reason or "")[:300], (actor or "")[:60], int(time.time())))
    db.commit()

def hist_list(tg_id, limit=6):
    rows = db.execute("select kind, ts, reason from ban_history where tg_id=? order by id desc limit ?", (tg_id, limit)).fetchall()
    return [[r["kind"], r["ts"] or 0, (r["reason"] or "")[:80]] for r in rows]

def track_msg(tg_id, msg_id):
    db.execute("insert into ban_msgs(tg_id,msg_id) values(?,?)", (tg_id, msg_id)); db.commit()

async def ban_reply(update, text, kb=None):
    """Ответ забаненному в Telegram: запоминаем сообщение, чтобы удалить его после разбана."""
    m = await update.message.reply_text(text, reply_markup=kb)
    track_msg(update.effective_user.id, m.message_id)
    return m

async def wipe_ban_msgs(tg_id):
    """После разбана убираем из чата все сообщения про депортацию. Старше 48 ч Telegram не даёт удалить: тогда стираем текст и кнопку."""
    tg = _hold.get("tg")
    rows = db.execute("select msg_id from ban_msgs where tg_id=?", (tg_id,)).fetchall()
    db.execute("delete from ban_msgs where tg_id=?", (tg_id,)); db.commit()
    if not tg:
        return
    for r in rows:
        try:
            await tg.bot.delete_message(chat_id=tg_id, message_id=r["msg_id"])
        except Exception:
            try:
                await tg.bot.edit_message_text(chat_id=tg_id, message_id=r["msg_id"], text="🕊 Сообщение устарело.", reply_markup=None)
            except Exception:
                pass

async def tg_dm(tg_id, text, p=None, track=False):
    """Личное сообщение игроку в Telegram с кнопкой открыть приложение."""
    tg = _hold.get("tg")
    if not tg:
        return False
    p = p or fresh(tg_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть приложение", web_app=WebAppInfo(url=webapp_link(p)))]]) if p else None
    try:
        m = await tg.bot.send_message(chat_id=tg_id, text=text, reply_markup=kb)
        if track:
            track_msg(tg_id, m.message_id)
        return True
    except Exception as e:
        log.warning("tg_dm %s: %s", tg_id, e)
        return False

async def do_ban(did, reason="", by=""):
    """Человека забанили в Discord: запоминаем бан и, если он привязан к игроку, банним его в приложении."""
    did = str(did)
    reason, by = (reason or "").strip(), (by or "").strip()
    db.execute("insert or replace into bans(discord_id,reason,ts,banned_by) values(?,?,?,?)", (did, reason, int(time.time()), by))
    p = by_discord(did)
    if not p or p["banned"]:
        db.commit()
        return None
    db.execute("update players set banned=1, ban_reason=?, ban_ts=?, ban_by=?, appeal_status='', appeal_text='', appeal_note='', appeal_ts=0 where tg_id=?",
               (reason, int(time.time()), by, p["tg_id"]))
    db.commit()
    _staff_cache.pop(p["tg_id"], None)
    p = fresh(p["tg_id"])
    hist_add(p, "ban", reason, by)
    await tg_dm(p["tg_id"], ban_text(p), p, track=True)
    await notify_admins(f"🚫 <@{did}> забанен(а) в Discord, в приложении аккаунт тоже заблокирован (Telegram: @{p['username'] or p['tg_id']})." + (f"\nПричина: {reason}" if reason else ""))
    return p

async def do_unban(did):
    """Бан в Discord сняли: снимаем и в приложении."""
    did = str(did)
    db.execute("delete from bans where discord_id=?", (did,))
    p = by_discord(did)
    if not p or not p["banned"]:
        db.commit()
        return None
    db.execute("update players set banned=0, appeal_status='', appeal_text='', appeal_note='', appeal_ts=0 where tg_id=?", (p["tg_id"],))
    db.commit()
    _staff_cache.pop(p["tg_id"], None)
    p = fresh(p["tg_id"])
    hist_add(p, "unban")
    await wipe_ban_msgs(p["tg_id"])
    await tg_dm(p["tg_id"], "✅ Бан снят. Добро пожаловать обратно в Вышеград! Зайди на сервер Discord, роли и имя вернутся сами.", p)
    await notify_admins(f"✅ <@{did}> разбанен(а), аккаунт в приложении снова активен.")
    return p

async def ban_info(guild, user):
    """Причина и кто забанил (из бана и журнала аудита). Нужны права «Банить участников» и «Просмотр журнала аудита»."""
    reason, by = "", ""
    try:
        reason = (await guild.fetch_ban(user)).reason or ""
    except Exception:
        pass
    try:
        async for en in guild.audit_logs(limit=10, action=discord.AuditLogAction.ban):
            if en.target and en.target.id == user.id:
                by = en.user.display_name if en.user else ""
                reason = reason or en.reason or ""
                break
    except Exception:
        pass
    return reason, by

async def sync_bans():
    """Сверка со списком банов Discord: ловит всё, что случилось, пока бот был выключен."""
    g = dc.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if not g:
        return
    ids = {}
    try:
        async for e in g.bans(limit=None):
            ids[str(e.user.id)] = e.reason or ""
    except Exception as e:
        log.warning("sync_bans: %s", e)
        return
    for did, r in ids.items():
        old = db.execute("select reason, banned_by from bans where discord_id=?", (did,)).fetchone()
        await do_ban(did, (old["reason"] if old and old["reason"] else r), (old["banned_by"] if old else ""))
    for row in db.execute("select discord_id from players where banned=1 and discord_id is not null").fetchall():
        if row["discord_id"] not in ids:
            await do_unban(row["discord_id"])
    for row in db.execute("select discord_id from bans").fetchall():
        if row["discord_id"] not in ids:
            db.execute("delete from bans where discord_id=?", (row["discord_id"],))
    db.commit()

async def ban_loop():
    await dc.wait_until_ready()
    while True:
        try:
            await sync_bans()
        except Exception:
            log.exception("ban_loop")
        await asyncio.sleep(300)

async def api_appeal(req):
    """Апелляция забаненного игрока: уходит админам в Discord с кнопками «Одобрить» / «Отклонить»."""
    if req.method == "OPTIONS":
        return web.Response(headers=CORS)
    p, body, err = await auth(req, True)
    if err:
        return err
    if not p["banned"]:
        return reply({"error": "notbanned"}, 400)
    if p["appeal_status"] == "wait":
        return reply({"error": "wait"}, 409)
    left = (p["appeal_ts"] or 0) + APPEAL_COOLDOWN - int(time.time())
    if p["appeal_status"] == "no" and left > 0:
        return reply({"error": "cool", "left": left}, 429)
    text = " ".join(str(body.get("text", "")).split())[:800]
    if len(text) < 10:
        return reply({"error": "short"}, 400)
    now = int(time.time())
    prev = (p["appeal_status"] or "", p["appeal_text"] or "", p["appeal_ts"] or 0)
    db.execute("update players set appeal_status='wait', appeal_text=?, appeal_ts=?, appeal_note='' where tg_id=?", (text, now, p["tg_id"]))
    db.commit()
    p = fresh(p["tg_id"])
    if not await post_appeal(p):                            # админы апелляцию не увидят: откатываем и честно говорим игроку
        db.execute("update players set appeal_status=?, appeal_text=?, appeal_ts=? where tg_id=?",
                   (prev[0], prev[1], prev[2], p["tg_id"])); db.commit()
        return reply({"error": "nochan"}, 503)
    hist_add(p, "appeal", text)
    return reply({"ok": 1, "ast": "wait", "apt": now, "apx": text[:300]})

async def post_appeal(p):
    cid = cfg("review_channel", "") or cfg("bank_channel", os.getenv("BANK_CHANNEL_ID", ""))
    if not cid:
        logging.warning("post_appeal: не задан канал, выполни /setreview в Discord")
        return False
    try:
        ch = dc.get_channel(int(cid)) or await dc.fetch_channel(int(cid))
        emb = discord.Embed(title="⚖️ Апелляция на бан", description=p["appeal_text"] or "—", color=0xC9973C)
        emb.add_field(name="Игрок", value=f"<@{p['discord_id']}> · Telegram @{p['username'] or p['tg_id']}", inline=False)
        emb.add_field(name="Причина бана", value=(p["ban_reason"] or "не указана")[:200], inline=False)
        emb.add_field(name="Забанил", value=p["ban_by"] or "—", inline=True)
        emb.add_field(name="Дело", value=ban_fields(p)["case"], inline=True)
        view = discord.ui.View(timeout=None)
        view.add_item(AppealBtn("ok", p["tg_id"]))
        view.add_item(AppealBtn("no", p["tg_id"]))
        await ch.send(embed=emb, view=view)
        return True
    except Exception as e:
        logging.warning("post_appeal: %s", e)
        return False

async def finish_appeal(i, tg_id, ok, note=""):
    p = fresh(tg_id)
    if not p or not p["banned"] or p["appeal_status"] != "wait":
        return await i.response.send_message("Эта апелляция уже обработана.", ephemeral=True)
    who = i.user.display_name
    if ok:
        try:
            await i.guild.unban(discord.Object(id=int(p["discord_id"])), reason=f"Апелляция одобрена ({who})")
        except discord.NotFound:
            pass
        except Exception as e:
            return await i.response.send_message(f"Не смог разбанить: {e}", ephemeral=True)
        hist_add(p, "appeal_ok", "", who)
        await do_unban(p["discord_id"])
    else:
        db.execute("update players set appeal_status='no', appeal_note=? where tg_id=?", (note.strip()[:200], tg_id))
        db.commit()
        p = fresh(tg_id)
        hist_add(p, "appeal_no", note, who)
        await tg_dm(tg_id, "⚖️ Апелляция отклонена." + (f"\n\nКомментарий: {note.strip()[:200]}" if note.strip() else "") +
                    f"\n\nПовторно подать можно через {APPEAL_COOLDOWN // 3600} ч.", p, track=True)
    emb = i.message.embeds[0] if i.message and i.message.embeds else discord.Embed(title="⚖️ Апелляция на бан")
    emb.color = 0x3BA55D if ok else 0xED4245
    emb.add_field(name="Решение", value=("✅ Одобрено, игрок разбанен" if ok else "❌ Отклонено" + (f": {note.strip()[:200]}" if note.strip() else "")) + f"\nРешил(а): {who}", inline=False)
    await i.response.edit_message(embed=emb, view=None)

# ---------------- Discord ----------------
class Bot(discord.Client):
    def __init__(self):
        it = discord.Intents.default()
        it.members = MEMBERS_INTENT
        it.message_content = True                           # нужно, чтобы читать текст машин в канале автосалона (включи Message Content Intent в портале)
        if hasattr(it, "moderation"):
            it.moderation = True                            # события бана и разбана (не привилегированный интент)
        else:
            it.bans = True
        super().__init__(intents=it)
        self.tree = app_commands.CommandTree(self)
    async def setup_hook(self):
        self.add_dynamic_items(AppealBtn)                   # кнопки апелляций работают и после перезапуска бота
        asyncio.create_task(ban_loop())
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
    p = fresh(r["tg_id"])
    nick = None
    if isinstance(i.user, discord.Member):
        nick = await apply_member(i.user, p["pp_name"] or p["name"], [NEWBIE_ROLE_ID, CITIZEN_ROLE_ID if p["pp_name"] else ""])
        db.execute("update players set reg_ts=? where tg_id=?", (int(time.time()), r["tg_id"])); db.commit()
        await notify_admins(f"✅ {i.user.mention} привязал(а) Discord по коду (Telegram: @{p['username'] or p['tg_id']}).")
    await i.response.send_message("Готово, Discord привязан. Вернись в приложение: проверка пройдена." + (f" Имя на сервере: {nick}." if nick else ""), ephemeral=True)

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
    log_tx(p["tg_id"], "admin", amount, col)
    await bank_log(f"🏦 Администрация: {member.mention} {amount:+d} {unit} ({where.name if where else 'Влеты'}). Баланс: {fmt(now)} {unit}")
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
        f"За 30 дней: {c} покупок на {fmt(s)} {CUR}" + ban_block(p)), ephemeral=True)

def ban_block(p):
    """Блок «История банов» для /info."""
    rows = db.execute("select kind, ts, reason, actor from ban_history where tg_id=? order by id desc limit 10", (p["tg_id"],)).fetchall()
    n = sum(1 for r in rows if r["kind"] == "ban")
    head = f"\n\n**История банов ({n})**" + (" · сейчас забанен" if p["banned"] else "")
    if not rows:
        return head + ": чисто" if not p["banned"] else head
    lines = [f"{time.strftime('%d.%m.%Y %H:%M', time.localtime(r['ts'] or 0))} · {HIST_TITLES.get(r['kind'], r['kind'])}"
             + (f" · {r['reason'][:80]}" if r["reason"] else "") + (f" ({r['actor']})" if r["actor"] else "") for r in rows]
    return head + "\n" + "\n".join(lines)

@tree.command(name="buys", description="Покупки игрока за последние 30 дней")
@admin
async def d_buys(i: discord.Interaction, member: discord.Member):
    p = await target(i, member)
    if not p: return
    rows = db.execute("select * from purchases where tg_id=? and ts>=? order by ts desc limit 25", (p["tg_id"], month_ago())).fetchall()
    text = "\n".join(f"{time.strftime('%d.%m', time.localtime(r['ts']))}  {r['item']}  {fmt(r['price'])}" for r in rows) or "Покупок нет."
    await i.response.send_message(cut(f"Покупки {member.mention} за 30 дней:\n{text}"), ephemeral=True)

# ---------------- экономика ----------------
def cfg(k, d=None):
    r = db.execute("select v from cfg where k=?", (k,)).fetchone()
    return r["v"] if r else d

def cfg_set(k, v):
    db.execute("insert into cfg(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (k, str(v))); db.commit()

tax_rate = lambda: int(cfg("tax", "13"))
treasury = lambda: int(cfg("treasury", "0"))

def log_tx(tg_id, kind, amount, note=""):
    db.execute("insert into tx(tg_id,ts,kind,amount,note) values(?,?,?,?,?)", (tg_id, int(time.time()), kind, amount, note)); db.commit()

async def bank_log(text):
    """Пишет строку в канал банка (если он задан). Ошибки не роняют бота."""
    cid = cfg("bank_channel", os.getenv("BANK_CHANNEL_ID", ""))
    if not cid:
        return
    try:
        ch = dc.get_channel(int(cid)) or await dc.fetch_channel(int(cid))
        await ch.send(cut(text))
    except Exception as e:
        logging.warning("bank_log: %s", e)

async def job_ac(i: discord.Interaction, cur: str):
    rows = db.execute("select name from jobs order by name").fetchall()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows if cur.lower() in r["name"].lower()][:25]

@tree.command(name="jobadd", description="Создать или изменить работу: зарплата, пауза между выплатами, мин. уровень")
@admin
async def d_jobadd(i: discord.Interaction, name: str, salary: int, cooldown_min: int = 60, min_level: int = 1):
    if not (1 <= salary <= 10**7 and 1 <= cooldown_min <= 10080 and 1 <= min_level <= 1000):
        return await i.response.send_message("Проверь числа: зарплата 1–10 000 000, пауза 1–10 080 мин, уровень 1–1000.", ephemeral=True)
    db.execute("insert into jobs(name,salary,cooldown,min_level) values(?,?,?,?) on conflict(name) do update set "
               "salary=excluded.salary, cooldown=excluded.cooldown, min_level=excluded.min_level",
               (name.strip()[:40], salary, cooldown_min, min_level)); db.commit()
    await i.response.send_message(f"Работа «{name.strip()[:40]}»: ${fmt(salary)} раз в {cooldown_min} мин, от {min_level} уровня.")

@tree.command(name="jobremove", description="Удалить работу из списка")
@app_commands.autocomplete(name=job_ac)
@admin
async def d_jobremove(i: discord.Interaction, name: str):
    n = db.execute("delete from jobs where name=?", (name,)).rowcount; db.commit()
    await i.response.send_message("Удалено." if n else "Такой работы нет.", ephemeral=not n)

@tree.command(name="jobs", description="Список работ и зарплат")
async def d_jobs(i: discord.Interaction):
    rows = db.execute("select * from jobs order by salary desc").fetchall()
    if not rows:
        return await i.response.send_message("Работ пока нет.", ephemeral=True)
    t = tax_rate()
    lines = [f"**{r['name']}**: ${fmt(r['salary'])} (на руки ${fmt(r['salary'] - r['salary'] * t // 100)}) раз в {r['cooldown']} мин, от {r['min_level']} ур." for r in rows]
    await i.response.send_message(cut(f"Налог на доход: {t}%\n" + "\n".join(lines)), ephemeral=True)

@tree.command(name="takejob", description="Устроиться на работу из списка")
@app_commands.autocomplete(name=job_ac)
async def d_takejob(i: discord.Interaction, name: str):
    p = by_discord(i.user.id)
    if not p: return await i.response.send_message("Сначала привяжи Telegram: /link.", ephemeral=True)
    j = db.execute("select * from jobs where name=?", (name,)).fetchone()
    if not j: return await i.response.send_message("Такой работы нет. Смотри /jobs.", ephemeral=True)
    if (p["level"] or 1) < j["min_level"]:
        return await i.response.send_message(f"Нужен уровень {j['min_level']}, у тебя {p['level'] or 1}.", ephemeral=True)
    db.execute("update players set job=?, last_work=0 where tg_id=?", (j["name"], p["tg_id"])); db.commit()
    await i.response.send_message(f"Ты устроился: {j['name']}. Получай зарплату командой /work.", ephemeral=True)

@tree.command(name="work", description="Получить зарплату (после налога, на карту)")
async def d_work(i: discord.Interaction):
    p = by_discord(i.user.id)
    if not p: return await i.response.send_message("Сначала привяжи Telegram: /link.", ephemeral=True)
    j = db.execute("select * from jobs where name=?", (p["job"],)).fetchone()
    if not j: return await i.response.send_message("У тебя нет работы из списка. Смотри /jobs и бери /takejob.", ephemeral=True)
    left = (p["last_work"] or 0) + j["cooldown"] * 60 - int(time.time())
    if left > 0:
        return await i.response.send_message(f"Рано. Следующая выплата через {left // 60 + 1} мин.", ephemeral=True)
    gross = j["salary"]; tax = gross * tax_rate() // 100; net = gross - tax
    works = (p["works"] or 0) + 1; lvl = p["level"] or 1; up = works % WORKS_PER_LEVEL == 0
    if up: lvl += 1                                          # каждые WORKS_PER_LEVEL выплат = +1 уровень
    db.execute("update players set bank=coalesce(bank,0)+?, last_work=?, works=?, level=? where tg_id=?", (net, int(time.time()), works, lvl, p["tg_id"]))
    db.execute("insert into cfg(k,v) values('treasury',?) on conflict(k) do update set v=cast(v as integer)+?", (str(tax), tax))
    db.commit()
    log_tx(p["tg_id"], "salary", net, f"{j['name']}, налог {tax}")
    bal = fresh(p["tg_id"])["bank"]
    await i.response.send_message(f"Зарплата ({j['name']}): ${fmt(gross)}, налог ${fmt(tax)}, на карту ${fmt(net)}. На карте: ${fmt(bal)}"
                                            + (f"\n⭐ Новый уровень: {lvl}!" if up else f"\nДо следующего уровня: {WORKS_PER_LEVEL - works % WORKS_PER_LEVEL} раб."), ephemeral=True)
    await bank_log(f"💼 {i.user.mention} получил зарплату ${fmt(net)} ({j['name']}, налог ${fmt(tax)})")

@tree.command(name="pay", description="Перевести деньги другому игроку с карты на карту")
async def d_pay(i: discord.Interaction, member: discord.Member, amount: int):
    p, q = by_discord(i.user.id), by_discord(member.id)
    if not p: return await i.response.send_message("Сначала привяжи Telegram: /link.", ephemeral=True)
    if not q: return await i.response.send_message(f"{member.mention} ещё не привязал Telegram.", ephemeral=True)
    if p["tg_id"] == q["tg_id"] or not (1 <= amount <= 10**9):
        return await i.response.send_message("Неверная сумма или получатель.", ephemeral=True)
    if (p["bank"] or 0) < amount:
        return await i.response.send_message(f"Не хватает денег на карте: ${fmt(p['bank'] or 0)}.", ephemeral=True)
    db.execute("update players set bank=coalesce(bank,0)-? where tg_id=?", (amount, p["tg_id"]))
    db.execute("update players set bank=coalesce(bank,0)+? where tg_id=?", (amount, q["tg_id"])); db.commit()
    log_tx(p["tg_id"], "pay_out", -amount, member.display_name); log_tx(q["tg_id"], "pay_in", amount, i.user.display_name)
    await i.response.send_message(f"Переведено ${fmt(amount)} → {member.mention}. Осталось: ${fmt(fresh(p['tg_id'])['bank'])}", ephemeral=True)
    await bank_log(f"💸 {i.user.mention} → {member.mention}: ${fmt(amount)}")

@tree.command(name="statement", description="Мои последние операции")
async def d_statement(i: discord.Interaction):
    p = by_discord(i.user.id)
    if not p: return await i.response.send_message("Сначала привяжи Telegram: /link.", ephemeral=True)
    rows = db.execute("select * from tx where tg_id=? order by id desc limit 12", (p["tg_id"],)).fetchall()
    text = "\n".join(f"{time.strftime('%d.%m %H:%M', time.localtime(r['ts']))}  {r['amount']:+,}  {r['kind']} {r['note'] or ''}".replace(",", " ") for r in rows)
    await i.response.send_message(cut(text or "Операций нет."), ephemeral=True)

@tree.command(name="tax", description="Установить налог на зарплату, %")
@admin
async def d_tax(i: discord.Interaction, percent: int):
    if not 0 <= percent <= 100:
        return await i.response.send_message("От 0 до 100.", ephemeral=True)
    cfg_set("tax", percent)
    await i.response.send_message(f"Налог на доход: {percent}%")

@tree.command(name="treasury", description="Сколько денег в казне (налоги)")
@admin
async def d_treasury(i: discord.Interaction):
    await i.response.send_message(f"Казна: ${fmt(treasury())}. Налог: {tax_rate()}%", ephemeral=True)

@tree.command(name="treasurypay", description="Выплатить игроку из казны на карту (пособие, премия)")
@admin
async def d_treasurypay(i: discord.Interaction, member: discord.Member, amount: int):
    p = await target(i, member)
    if not p: return
    if not (1 <= amount <= treasury()):
        return await i.response.send_message(f"В казне ${fmt(treasury())}, сумма неверная.", ephemeral=True)
    cfg_set("treasury", treasury() - amount)
    db.execute("update players set bank=coalesce(bank,0)+? where tg_id=?", (amount, p["tg_id"])); db.commit()
    log_tx(p["tg_id"], "treasury", amount, "из казны")
    await i.response.send_message(f"{member.mention}: +${fmt(amount)} из казны. В казне: ${fmt(treasury())}")
    await bank_log(f"🏛 Казна → {member.mention}: ${fmt(amount)}")

@tree.command(name="setbank", description="Канал, куда бот пишет операции банка")
@admin
async def d_setbank(i: discord.Interaction, channel: discord.TextChannel):
    cfg_set("bank_channel", channel.id)
    await i.response.send_message(f"Операции банка будут в {channel.mention}.")

@dc.event
async def on_member_join(member):
    """Новичок зашёл на сервер: если он уже прошёл проверку в приложении, бот сам ставит имя и роль."""
    if GUILD_ID and str(member.guild.id) != str(GUILD_ID):
        return
    p = by_discord(member.id)
    if p:
        await apply_member(member, p["pp_name"] or p["name"], [NEWBIE_ROLE_ID, CITIZEN_ROLE_ID if p["pp_name"] else ""])
        await notify_admins(f"👋 {member.mention} вернулся(ась) на сервер, имя и роли выданы заново.")
    else:
        await notify_admins(f"👋 {member.mention} зашёл(ла) на сервер. Ждём проверку в приложении.")

@tree.command(name="approve", description="Принять игрока после проверки анкеты: бот напишет ему в Telegram и пришлёт приветствие")
@admin
async def d_approve(i: discord.Interaction, member: discord.Member):
    p = await target(i, member)
    if not p:
        return
    again = bool(p["approved"])
    db.execute("update players set approved=1, approved_ts=? where tg_id=?", (int(time.time()), p["tg_id"])); db.commit()
    ok = await send_welcome(fresh(p["tg_id"]))
    msg = f"{member.mention} принят(а)." if not again else f"{member.mention} уже был(а) принят(а), приветствие отправлено ещё раз."
    if not ok:
        msg += " В Telegram написать не получилось: игрок мог не нажать /start или заблокировал бота."
    await i.response.send_message(msg, ephemeral=True)

@tree.command(name="setreview", description="Канал, куда бот пишет новичков и заполненные анкеты для проверки")
@admin
async def d_setreview(i: discord.Interaction, channel: discord.TextChannel):
    cfg_set("review_channel", channel.id)
    await i.response.send_message(f"Проверка новичков и анкет будет в {channel.mention}.")

# ---------------- сброс ----------------
epoch = lambda: cfg("epoch", "1")          # меняется при общем сбросе: приложение у игроков забывает старое состояние
RESET_SET = ("discord_id=NULL, pp_name=NULL, pp_data=NULL, reg_data=NULL, form_ok=0, form_ts=0, reg_ts=0, approved=0, approved_ts=0, "
             "rejected=0, reject_reason='', decided_ts=0, decided_by=NULL")

def wipe_passports(ids=None):
    try:
        for f in os.listdir(PASS_DIR):
            if f.endswith(".png") and (ids is None or f[:-4] in ids):
                os.remove(os.path.join(PASS_DIR, f))
    except FileNotFoundError:
        pass

@tree.command(name="resetall", description="Сбросить ВСЕХ: Discord-проверка, паспорт, анкета, приём. Деньги, работа и имущество остаются")
@admin
async def d_resetall(i: discord.Interaction, confirm: str):
    if confirm.strip().upper() != "СБРОС":
        return await i.response.send_message("Это сбросит проверку у всех игроков. Чтобы подтвердить, введи в поле confirm слово: СБРОС", ephemeral=True)
    n = db.execute(f"update players set {RESET_SET}").rowcount
    db.execute("delete from links")
    db.commit()
    cfg_set("epoch", int(time.time()))
    _staff_cache.clear(); _ans_cache.clear()
    wipe_passports()
    await i.response.send_message(f"Сброшено игроков: {n}. Теперь все проходят путь заново: Discord, паспорт, анкета. Деньги, работа и имущество не тронуты. "
                                  "Не забудь удалить старые ответы в Google Форме (Ответы → Удалить все).", ephemeral=True)
    await notify_admins(f"♻️ {i.user.mention} сбросил(а) проверку у всех игроков ({n}).")

@tree.command(name="reset", description="Сбросить одного игрока: Discord-проверка, паспорт, анкета, приём")
@admin
async def d_reset(i: discord.Interaction, member: discord.Member):
    p = await target(i, member)
    if not p:
        return
    db.execute(f"update players set {RESET_SET} where tg_id=?", (p["tg_id"],)); db.commit()
    _staff_cache.pop(p["tg_id"], None); _ans_cache.pop(p["tg_id"], None)
    wipe_passports({str(p["tg_id"])})
    await i.response.send_message(f"{member.mention}: проверка сброшена. Пусть напишет боту /start и пройдёт путь заново.", ephemeral=True)


class AppealBtn(discord.ui.DynamicItem[discord.ui.Button], template=r"appeal:(?P<act>ok|no):(?P<tg>\d+)"):
    def __init__(self, act, tg):
        ok = act == "ok"
        super().__init__(discord.ui.Button(label="Одобрить и разбанить" if ok else "Отклонить", emoji="✅" if ok else "❌",
                                           style=discord.ButtonStyle.success if ok else discord.ButtonStyle.danger,
                                           custom_id=f"appeal:{act}:{tg}"))
        self.act, self.tg = act, int(tg)
    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], int(match["tg"]))
    async def callback(self, i: discord.Interaction):
        if not (isinstance(i.user, discord.Member) and (i.user.guild_permissions.ban_members or i.user.guild_permissions.administrator)):
            return await i.response.send_message("Решать апелляции могут только те, у кого есть право банить участников.", ephemeral=True)
        if self.act == "ok":
            await finish_appeal(i, self.tg, True)
        else:
            await i.response.send_modal(DenyModal(self.tg))

class DenyModal(discord.ui.Modal, title="Отклонить апелляцию"):
    note = discord.ui.TextInput(label="Что написать игроку (необязательно)", required=False, max_length=200, style=discord.TextStyle.paragraph)
    def __init__(self, tg):
        super().__init__()
        self.tg = tg
    async def on_submit(self, i: discord.Interaction):
        await finish_appeal(i, self.tg, False, str(self.note.value or ""))

@dc.event
async def on_member_ban(guild, user):
    if GUILD_ID and str(guild.id) != str(GUILD_ID):
        return
    await asyncio.sleep(1.5)                                # журнал аудита обновляется с небольшой задержкой
    reason, by = await ban_info(guild, user)
    await do_ban(user.id, reason, by)

@dc.event
async def on_member_unban(guild, user):
    if GUILD_ID and str(guild.id) != str(GUILD_ID):
        return
    await do_unban(user.id)

# ---------------- запуск ----------------
async def main():
    app = Application.builder().token(TG_TOKEN).build()
    for name, fn in (("start", tg_start), ("admin", tg_admin), ("myid", tg_myid), ("link", tg_link), ("history", tg_history), ("taxipay", tg_taxipay), ("taxirefund", tg_taxirefund)):
        app.add_handler(CommandHandler(name, fn))
    _hold["tg"] = app
    web_app = web.Application(client_max_size=5 * 1024 * 1024)
    web_app["tg"] = app
    for path, fn in (("/api/me", api_me), ("/api/sync", api_sync), ("/api/transfer", api_transfer), ("/api/roblox", api_roblox),
                     ("/api/passport", api_passport), ("/api/discord", api_discord),
                     ("/api/linkcode", api_linkcode), ("/api/form", api_form),
                     ("/api/admin/list", api_admin_list), ("/api/admin/get", api_admin_get), ("/api/admin/decide", api_admin_decide),
                     ("/api/admin/answers", api_admin_answers), ("/api/appeal", api_appeal),
                     ("/api/admin/exam", api_admin_exam), ("/api/admin/license", api_admin_license)):
        web_app.router.add_route("*", path, fn)
    async def taxi_notify(text):
        for aid in ADMIN_IDS:
            try:
                await app.bot.send_message(chat_id=aid, text=text)
            except Exception:
                log.warning("taxi ping failed for %s", aid)
    setup_taxi(web_app, auth, db, log_tx, reply, CORS, taxi_notify)
    setup_market(web_app, auth, db, log_tx, reply, CORS)
    setup_salon(web_app, dc, db, auth, reply, CORS, fmt, log_tx, bank_log, GUILD_ID)   # вкладка «Салоны» в Моторсе
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
