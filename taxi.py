"""Живое такси: заказ -> предложения таксистов (время подачи) -> выбор клиента -> поездка -> оплата.
Всё хранится в памяти процесса (заказы живут минуты). Подключение: setup_taxi(web_app, auth, db, log_tx, reply, CORS)."""
import asyncio
import math
import time
from aiohttp import web

CLS_MULT = [1, 1.4, 2, 1.8]      # как CLASSES в map.html
PRICE_PER_100PX, MIN_PRICE = 25, 50
CONFIRM_TTL = 120_000            # клиент не ответил на «Приехали» за 2 минуты: оплата проходит сама
OPEN_TTL = 300_000               # заказ без выбора живёт 5 минут
DRV_TTL = 20_000                 # водитель без опроса дольше 20 с считается ушедшим со смены
ORDERS, DRIVERS, CARPH = {}, {}, {}   # CARPH: фото машин водителей (jpeg data-url)
_seq = [0]
_OPS = {}


def now():
    return int(time.time() * 1000)


def _s(v, n):
    return "".join(c for c in str(v or "") if c.isprintable())[:n].strip()


def _pt(v):
    try:
        x, y = float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return [round(x, 1), round(y, 1)] if 0 <= x <= 825 and 0 <= y <= 822 else None


def _int(v, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def price_of(a, b, k):
    d = math.hypot(a[0] - b[0], a[1] - b[1]) / 100 * PRICE_PER_100PX * CLS_MULT[k]
    return max(MIN_PRICE, int(d / 5 + 0.5) * 5)


def pub(o, uid):
    r = {k: o[k] for k in ("id", "a", "b", "an", "bn", "cls", "price", "pay", "comment", "pname", "status", "seg")}
    r["ev"] = o.get("ev", "")
    r["drv"] = o["drv"]
    if o["pid"] == uid:
        r["offers"] = list(o["offers"].values())
    else:
        r["mine"] = o["offers"].get(uid)
        r["n"] = len(o["offers"])
    return r


def sweep(t, charge, refund):
    for d in [d for d, v in DRIVERS.items() if t - v["t"] > DRV_TTL]:
        del DRIVERS[d]
    for o in list(ORDERS.values()):
        for d in [d for d in o["offers"] if d not in DRIVERS]:
            del o["offers"][d]
        if o["status"] == "open" and t - o["ts"] > OPEN_TTL:
            o["status"] = "cancel"
        elif o["status"] in ("to", "here") and t - o.get("dseen", t) > 180_000:      # водитель пропал: деньги назад
            refund(o)
            o["status"] = "cancel"
        elif o["status"] == "ride" and t - o.get("dseen", t) > 1_800_000:            # поездка была, водитель не закрыл
            finish(o, charge)
        elif o["status"] == "confirm" and t - o["cend"] > CONFIRM_TTL:               # клиент молчит: платим водителю
            finish(o, charge)
        if o["status"] in ("done", "cancel") and t - o.get("end", o["ts"]) > 180_000:
            del ORDERS[o["id"]]
        elif o["status"] in ("done", "cancel") and "end" not in o:
            o["end"] = t


def finish(o, charge):
    if o["status"] in ("done", "cancel"):
        return
    o["status"] = "done"
    o["result"] = "paid"
    o["end"] = now()
    charge(o)


def view(uid, t):
    out = {"t": t, "nd": len(DRIVERS), "open": [], "p": None, "d": None}
    for o in ORDERS.values():
        if o["pid"] == uid and o["status"] in ("open", "to", "here", "ride", "confirm"):
            out["p"] = pub(o, uid)
            o["ev"] = ""
        if o["driver"] == uid and o["status"] in ("to", "here", "ride", "confirm"):
            o["dseen"] = t
            out["d"] = pub(o, uid)
        if uid in DRIVERS and o["status"] == "open" and o["pid"] != uid:
            out["open"].append(pub(o, uid))
    last = [o for o in ORDERS.values() if uid in (o["pid"], o["driver"]) and o["status"] in ("done", "dispute")
            and t - o.get("end", 0) < 180_000]
    if last:
        o = max(last, key=lambda x: x["end"])
        out["last"] = {"id": o["id"], "result": "dispute" if o["status"] == "dispute" else "paid", "price": o["price"]}
    return out


def setup_taxi(web_app, auth, db, log_tx, reply, CORS, notify=None):
    def funds(pid):
        r = db.execute("select coalesce(cash,0), coalesce(bank,0) from players where tg_id=?", (pid,)).fetchone()
        return (r[0], r[1]) if r else (0, 0)

    def hold(o):
        """Резерв: при выборе водителя деньги снимаются с пассажира (сначала способ оплаты, потом второй)."""
        cash, bank = funds(o["pid"])
        have = {"cash": cash, "bank": bank}
        first, other = ("cash", "bank") if o["pay"] == "Наличные" else ("bank", "cash")
        left, held = o["price"], {}
        for col in (first, other):
            take = min(left, have[col])
            if take > 0:
                db.execute(f"update players set {col}=coalesce({col},0)-? where tg_id=?", (take, o["pid"]))
                held[col] = take
                left -= take
        db.commit()
        o["held"] = held

    def refund(o):
        for col, v in (o.get("held") or {}).items():
            db.execute(f"update players set {col}=coalesce({col},0)+? where tg_id=?", (v, o["pid"]))
        db.commit()
        o["held"] = {}

    def charge(o):
        """Поездка выполнена: резерв уходит водителю."""
        paid = sum((o.get("held") or {}).values())
        o["held"] = {}
        if paid:
            db.execute("update players set cash=coalesce(cash,0)+? where tg_id=?", (paid, o["driver"]))
            db.commit()
            log_tx(o["pid"], "taxi", -paid, f"Такси: {o['an']} → {o['bn']}")
            log_tx(o["driver"], "taxi", paid, f"Такси: {o['an']} → {o['bn']}")

    async def api_taxi(req):
        if req.method == "OPTIONS":
            return web.Response(headers=CORS)
        p, body, err = await auth(req)
        if err:
            return err
        uid, act, t = p["tg_id"], body.get("act"), now()
        sweep(t, charge, refund)
        name = _s(body.get("name"), 24) or "Игрок"
        oid = _int(body.get("oid"), 1, 10**9)
        o = ORDERS.get(oid)

        if act == "photo":      # фото машины водителя по запросу (в предложения оно не вшивается, чтобы не раздувать опрос)
            return reply({"photo": CARPH.get(_int(body.get("did"), 1, 10**15))})

        if act == "poll":
            ph = body.get("photo")
            if isinstance(ph, str) and ph.startswith("data:image/jpeg;base64,") and len(ph) <= 120_000 and len(CARPH) < 500:
                CARPH[uid] = ph
            elif ph == "":
                CARPH.pop(uid, None)
            if body.get("shift"):
                pos = _pt(body.get("pos"))
                d = DRIVERS.setdefault(uid, {"pos": pos or [455, 455]})
                d.update(t=t, name=name, car=_s(body.get("car"), 30) or "Машина", plate=_s(body.get("plate"), 12))
                if pos:
                    d["pos"] = pos
            else:
                DRIVERS.pop(uid, None)

        elif act == "order":
            a, b, k = _pt(body.get("a")), _pt(body.get("b")), _int(body.get("cls"), 0, 3)
            if not a or not b or k is None or math.hypot(a[0] - b[0], a[1] - b[1]) < 5:
                return reply({"error": "bad"}, 400)
            for x in ORDERS.values():
                if x["pid"] == uid and x["status"] in ("open", "to"):
                    x["status"] = "cancel"
                elif x["pid"] == uid and x["status"] in ("here", "ride", "confirm"):
                    return reply({"error": "busy"}, 409)
            _seq[0] += 1
            ORDERS[_seq[0]] = {"id": _seq[0], "pid": uid, "pname": name, "a": a, "b": b, "an": _s(body.get("an"), 40),
                               "bn": _s(body.get("bn"), 40), "cls": k, "price": price_of(a, b, k),
                               "pay": _s(body.get("pay"), 20) or "Наличные", "comment": _s(body.get("comment"), 120),
                               "status": "open", "ts": t, "offers": {}, "driver": None, "drv": None, "seg": None}

        elif act == "offer":
            eta, rsec, pos = _int(body.get("eta"), 3, 5400), _int(body.get("rsec"), 3, 10800), _pt(body.get("pos"))
            busy = any(x["driver"] == uid and x["status"] in ("to", "here", "ride", "confirm") for x in ORDERS.values())
            if not o or o["status"] != "open" or o["pid"] == uid or uid not in DRIVERS or busy or not eta or not rsec or not pos:
                return reply({"error": "bad"}, 400)
            d = DRIVERS[uid]
            o["offers"][uid] = {"did": uid, "name": d["name"], "car": d["car"], "plate": d["plate"], "pos": pos,
                                "eta": eta, "rsec": rsec, "ph": uid in CARPH}

        elif act == "unoffer":
            if o:
                o["offers"].pop(uid, None)

        elif act == "choose":
            of = o and o["offers"].get(_int(body.get("did"), 1, 10**15))
            if not o or o["pid"] != uid or o["status"] != "open" or not of:
                return reply({"error": "bad"}, 400)
            if sum(funds(uid)) < o["price"]:
                return reply({"error": "funds"}, 402)
            o.update(status="to", driver=of["did"], dseen=t, seg={"f": of["pos"], "t0": t, "t1": t + of["eta"] * 1000},
                     drv={k: of[k] for k in ("did", "name", "car", "plate", "eta", "rsec", "ph")} | {"from": of["pos"]})
            o["offers"] = {}
            hold(o)

        elif act == "cancel":
            if o and o["pid"] == uid and o["status"] in ("open", "to", "here"):
                refund(o)
                o["status"] = "cancel"
            elif o and o["driver"] == uid and o["status"] in ("to", "here"):
                refund(o)
                o.update(status="open", driver=None, drv=None, seg=None, ev="drv_cancel", ts=t)
            else:
                return reply({"error": "bad"}, 400)

        elif act in ("pos", "arrive", "start", "fin"):
            if not o or o["driver"] != uid:
                return reply({"error": "bad"}, 400)
            left = (o["seg"]["t1"] - t) // 1000 if o.get("seg") else 0
            if act == "pos":        # водитель обновил положение: машина едет дальше от новой точки
                pos, eta = _pt(body.get("pos")), _int(body.get("eta"), 3, 14400)
                if o["status"] not in ("to", "ride") or not pos or not eta:
                    return reply({"error": "bad"}, 400)
                o["seg"] = {"f": pos, "t0": t, "t1": t + eta * 1000}
            elif act == "arrive" and o["status"] == "to":
                if left > 5:        # раньше расчётного времени нельзя
                    return reply({"error": "early", "left": left}, 425)
                o["status"] = "here"
                o["seg"] = {"f": o["a"], "t0": t, "t1": t}
            elif act == "start" and o["status"] == "here":
                o["status"] = "ride"
                o["rs"] = t
                o["seg"] = {"f": o["a"], "t0": t, "t1": t + o["drv"]["rsec"] * 1000}
            elif act == "fin" and o["status"] == "ride":
                if left > 5:
                    return reply({"error": "early", "left": left}, 425)
                o["status"] = "confirm"     # ждём подтверждения клиента, деньги в резерве
                o["cend"] = t
            else:
                return reply({"error": "bad"}, 400)

        elif act in ("ok", "dispute"):
            if not o or o["pid"] != uid or o["status"] != "confirm":
                return reply({"error": "bad"}, 400)
            if act == "ok":
                finish(o, charge)
            else:
                o["status"] = "dispute"
                o["end"] = t
                if notify:
                    asyncio.get_event_loop().create_task(notify(
                        f"⚠️ Спор по такси №{o['id']}: {o['pname']} оспорил поездку {o['an']} → {o['bn']} ({o['price']}). "
                        f"Водитель id {o['driver']}. Деньги в резерве. /taxipay {o['id']} выплатить водителю, /taxirefund {o['id']} вернуть клиенту."))
        else:
            return reply({"error": "bad"}, 400)

        return reply(view(uid, t))

    def resolve(oid, pay):
        """Решение админа по спору: pay=True выплатить водителю, False вернуть клиенту. Возвращает текст-ответ."""
        o = ORDERS.get(oid)
        if not o or o["status"] != "dispute":
            return "Спора с таким номером нет (или уже решён)."
        o["end"] = now()
        if pay:
            o["status"] = "done"
            o["result"] = "paid"
            charge(o)
            return f"Такси №{oid}: деньги выплачены водителю."
        refund(o)
        o["status"] = "cancel"
        return f"Такси №{oid}: деньги вернули клиенту."

    _OPS["resolve"] = resolve
    web_app.router.add_route("*", "/api/taxi", api_taxi)
