"""Авторынок: объявления о продаже машин.
Машина при выставлении уходит в «эскроу» (убирается из players.cars), при покупке переходит покупателю,
при снятии с продажи возвращается владельцу. Деньги: у покупателя сначала наличные, потом карта; продавцу — в наличные.
API: POST /api/market {initData, act: list|photo|sell|buy|cancel}"""
import asyncio
import json
import time
from aiohttp import web

MAX_ACTIVE = 5          # объявлений одновременно у одного игрока
MAX_PRICE = 10 ** 9
MAX_PHOTO = 120_000     # символов data:image в base64


def setup_market(web_app, auth, db, log_tx, reply, CORS, hook=None):
    """hook(ev, row, photo='', buyer='') - зеркало в Discord: ev = new | sold | cancel (необязательно)."""
    db.executescript("""
    create table if not exists market(
      id integer primary key autoincrement, seller integer, sname text, car text, price integer,
      note text, photo text, ts integer, status text default 'open', buyer integer, sold_ts integer);
    create index if not exists market_status on market(status, ts);
    """)
    try:
        db.execute("alter table market add column thread text")
        db.commit()
    except Exception:
        pass

    async def fire(ev, row, photo="", buyer="", wait=False):
        if not hook:
            return
        try:
            if wait:
                await hook(ev, row, photo, buyer)
            else:
                asyncio.create_task(hook(ev, row, photo, buyer))
        except Exception:
            pass

    def cars_of(uid):
        r = db.execute("select cars from players where tg_id=?", (uid,)).fetchone()
        try:
            v = json.loads(r["cars"]) if r and r["cars"] else []
            return [x for x in v if isinstance(x, str)]
        except Exception:
            return []

    def put_cars(uid, lst):
        db.execute("update players set cars=? where tg_id=?", (json.dumps(lst, ensure_ascii=False), uid))

    def funds(uid):
        r = db.execute("select coalesce(cash,0) c, coalesce(bank,0) b from players where tg_id=?", (uid,)).fetchone()
        return (r["c"], r["b"]) if r else (0, 0)

    def pub(r, uid):
        return {"id": r["id"], "car": r["car"], "price": r["price"], "sname": r["sname"] or "Игрок",
                "note": r["note"] or "", "ph": 1 if r["photo"] else 0, "mine": 1 if r["seller"] == uid else 0,
                "ts": r["ts"]}

    def sname_of(p):
        return (p["name"] or (("@" + p["username"]) if p["username"] else "Игрок"))[:30]

    async def api_market(req):
        if req.method == "OPTIONS":
            return web.Response(headers=CORS)
        p, body, err = await auth(req)
        if err:
            return err
        uid, act = p["tg_id"], body.get("act")

        if act == "list":
            rows = db.execute("select * from market where status='open' order by id desc limit 120").fetchall()
            items = [pub(r, uid) for r in rows]
            return reply({"items": [i for i in items if not i["mine"]], "mine": [i for i in items if i["mine"]]})

        if act == "photo":
            r = db.execute("select photo from market where id=? and status='open'", (int(body.get("id") or 0),)).fetchone()
            return reply({"photo": r["photo"] if r and r["photo"] else ""})

        if act == "sell":
            car = str(body.get("car") or "").strip()[:80]
            try:
                price = int(body.get("price"))
            except Exception:
                return reply({"error": "price"}, 400)
            if not 1 <= price <= MAX_PRICE:
                return reply({"error": "price"}, 400)
            note = str(body.get("note") or "").strip()[:120]
            photo = body.get("photo") or ""
            if photo and (not isinstance(photo, str) or not photo.startswith("data:image/") or len(photo) > MAX_PHOTO):
                photo = ""
            n = db.execute("select count(*) c from market where seller=? and status='open'", (uid,)).fetchone()["c"]
            if n >= MAX_ACTIVE:
                return reply({"error": "limit"}, 409)
            lst = cars_of(uid)
            if car not in lst:
                return reply({"error": "nocar"}, 404)
            lst.remove(car)
            put_cars(uid, lst)
            cur = db.execute("insert into market(seller,sname,car,price,note,photo,ts) values(?,?,?,?,?,?,?)",
                             (uid, sname_of(p), car, price, note, photo, int(time.time())))
            db.commit()
            await fire("new", {"id": cur.lastrowid, "seller": uid, "sname": sname_of(p), "car": car,
                               "price": price, "note": note}, photo, wait=True)
            return reply({"ok": 1, "cars": lst})

        if act == "cancel":
            r = db.execute("select * from market where id=? and seller=? and status='open'",
                           (int(body.get("id") or 0), uid)).fetchone()
            if not r:
                return reply({"error": "bad"}, 404)
            lst = cars_of(uid)
            lst.append(r["car"])
            put_cars(uid, lst)
            db.execute("update market set status='cancel', sold_ts=? where id=?", (int(time.time()), r["id"]))
            db.commit()
            await fire("cancel", {k: r[k] for k in ("id", "seller", "sname", "car", "price")})
            return reply({"ok": 1, "cars": lst})

        if act == "buy":
            r = db.execute("select * from market where id=? and status='open'", (int(body.get("id") or 0),)).fetchone()
            if not r:
                return reply({"error": "gone"}, 404)
            if r["seller"] == uid:
                return reply({"error": "own"}, 400)
            cash, bank = funds(uid)
            if cash + bank < r["price"]:
                return reply({"error": "funds"}, 402)
            from_cash = min(cash, r["price"])
            from_bank = r["price"] - from_cash
            db.execute("update players set cash=coalesce(cash,0)-?, bank=coalesce(bank,0)-? where tg_id=?",
                       (from_cash, from_bank, uid))
            db.execute("update players set cash=coalesce(cash,0)+? where tg_id=?", (r["price"], r["seller"]))
            lst = cars_of(uid)
            lst.append(r["car"])
            put_cars(uid, lst)
            db.execute("update market set status='sold', buyer=?, sold_ts=? where id=?", (uid, int(time.time()), r["id"]))
            db.commit()
            log_tx(uid, "market", -r["price"], f"Авторынок: куплено {r['car']}")
            log_tx(r["seller"], "market", r["price"], f"Авторынок: продано {r['car']}")
            await fire("sold", {k: r[k] for k in ("id", "seller", "sname", "car", "price")}, buyer=sname_of(p))
            cash, bank = funds(uid)
            return reply({"ok": 1, "cash": cash, "bank": bank, "cars": lst})

        return reply({"error": "bad"}, 400)

    web_app.router.add_route("*", "/api/market", api_market)
