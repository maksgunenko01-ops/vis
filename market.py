"""Авторынок: объявления о продаже машин.
Машина при выставлении уходит в «эскроу» (убирается из players.cars), при покупке переходит покупателю,
при снятии с продажи возвращается владельцу. Деньги: у покупателя сначала наличные, потом карта; продавцу — в наличные.
API: POST /api/market {initData, act: list|photo|sell|buy|cancel}"""
import json
import time
from aiohttp import web

MAX_ACTIVE = 5          # объявлений одновременно у одного игрока
MAX_PRICE = 10 ** 9
MAX_PHOTO = 120_000     # символов data:image в base64


def setup_market(web_app, auth, db, log_tx, reply, CORS, market_discord=None):
    db.executescript("""
    create table if not exists market(
      id integer primary key autoincrement, seller integer, sname text, car text, price integer,
      note text, photo text, ts integer, status text default 'open', buyer integer, sold_ts integer);
    create index if not exists market_status on market(status, ts);
    """)

    def cars_of(uid):
        r = db.execute("select cars from players where tg_id=?", (uid,)).fetchone()
        try:
            v = json.loads(r["cars"]) if r and r["cars"] else []
            return [x for x in v if isinstance(x, str)]
        except Exception:
            return []

    def norm(x):
        return " ".join(str(x).split()).casefold()

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
            return reply({"items": [i for i in items if not i["mine"]], "mine": [i for i in items if i["mine"]],
                          "cars": cars_of(uid)})

        if act == "photo":
            r = db.execute("select photo from market where id=? and status='open'", (int(body.get("id") or 0),)).fetchone()
            return reply({"photo": r["photo"] if r and r["photo"] else ""})

        if act == "sell":
            car = str(body.get("car") or "").strip()
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
            real = next((x for x in lst if norm(x) == norm(car)), None)
            if real is None:
                return reply({"error": "nocar"}, 404)
            car = real
            lst.remove(real)
            put_cars(uid, lst)
            db.execute("insert into market(seller,sname,car,price,note,photo,ts) values(?,?,?,?,?,?,?)",
                       (uid, sname_of(p), car, price, note, photo, int(time.time())))
            db.commit()
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
            cash, bank = funds(uid)
            return reply({"ok": 1, "cash": cash, "bank": bank, "cars": lst})

        return reply({"error": "bad"}, 400)

    web_app.router.add_route("*", "/api/market", api_market)
