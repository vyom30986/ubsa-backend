"""Universal Book Selling Application - FastAPI backend.
Run:  uvicorn main:app --reload --port 8000
Docs: http://localhost:8000/docs
Works with NO API key. Set GROQ_API_KEY in .env to enable LLM phrasing.
"""
import os
import re
import uuid
import logging
from typing import Optional, List
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlmodel import Session, select

from db import engine, init_db, get_session
import models as M
from engine import (recommend, extract_slots, should_recommend, next_question,
                    mature_content_requested, child_safe, converse, select_books, synopsis,
                    requested_title_book, specific_title_response)
from seed import seed_demo, seed_history, seed_catalog

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

GROQ_KEY = os.getenv("GROQ_API_KEY")            # optional
ADMIN_PIN = os.getenv("ADMIN_PIN", "2410")      # TODO: per-store, configurable in admin settings
app = FastAPI(title="Universal Book Selling Application", version="1.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

# In-memory per-session discovery state (resets on restart - fine for the kiosk).
SESSION_STATE = {}


@app.on_event("startup")
def _startup():
    init_db()
    seed_demo()
    seed_history()


# ---------- helpers ----------
def sid(prefix): return f"{prefix}_{uuid.uuid4().hex[:8]}"
def mask(p):
    p = (p or "").strip()
    return ("x" * (len(p) - 4) + p[-4:]) if len(p) >= 4 else "xxxx"
def rupees(paise): return f"₹{paise // 100}"

def book_dict(s: Session, b: M.Book):
    inv = s.exec(select(M.Inventory).where(M.Inventory.book_id == b.id,
                                           M.Inventory.store_id == b.store_id)).first()
    qty = inv.quantity if inv else 0
    return {"id": b.id, "title": b.title, "author": b.author, "genre": b.genre,
            "price": b.price, "price_label": rupees(b.price), "age": b.age,
            "tags": b.tags, "shelf": b.shelf, "aisle": b.aisle,
            "in_stock": qty > 0, "quantity": qty}


# ---------- health ----------
@app.get("/api/v1/health")
def health():
    return {"ok": True, "llm": "groq" if GROQ_KEY else "rule-based (no key)"}


# ---------- stores ----------
class StoreIn(BaseModel):
    shop_name: str
    owner_name: str = "Owner"
    city: str = ""
    business_type: str = "independent_bookstore"
    currency: str = "INR"
    greeting: str = ""
    accent_color: str = "#9A6A45"
    languages: List[str] = ["en", "hi"]
    genres_stocked: List[str] = []

@app.post("/api/v1/stores", status_code=201)
def create_store(body: StoreIn, s: Session = Depends(get_session)):
    store = M.Store(id=sid("st"), shop_name=body.shop_name, owner_name=body.owner_name,
                    city=body.city, business_type=body.business_type, currency=body.currency,
                    greeting=body.greeting or f"Welcome to {body.shop_name}! What are you in the mood to read today?",
                    accent_color=body.accent_color, languages=",".join(body.languages),
                    genres_stocked=",".join(body.genres_stocked))
    s.add(store); s.commit(); s.refresh(store)
    seed_catalog(store.id)   # give the new store the starter catalog so the kiosk works
    return store

@app.get("/api/v1/stores/{store_id}")
def get_store(store_id: str, s: Session = Depends(get_session)):
    store = s.get(M.Store, store_id)
    if not store: raise HTTPException(404, "store not found")
    return store


# ---------- catalog & inventory ----------
@app.get("/api/v1/stores/{store_id}/books/search")
def search_books(store_id: str, q: str = "", genre: str = "", limit: int = 10,
                 s: Session = Depends(get_session)):
    stmt = select(M.Book).where(M.Book.store_id == store_id)
    if genre:
        stmt = stmt.where(M.Book.genre == genre)
    books = s.exec(stmt).all()
    if q:
        ql = q.lower()
        books = [b for b in books if ql in (b.title + b.author + b.tags + b.genre).lower()]
    return {"results": [book_dict(s, b) for b in books[:limit]]}

class CatalogCSV(BaseModel):
    csv: str

@app.post("/api/v1/stores/{store_id}/catalog/import")
def import_catalog_upload(store_id: str, body: CatalogCSV, s: Session = Depends(get_session)):
    """Owner uploads their inventory as CSV text. Replaces the store's catalogue.
    Columns: title,author,genre,price_rupees,age,tags,shelf,aisle,quantity"""
    import csv as _csv, io
    reader = _csv.DictReader(io.StringIO(body.csv))
    for b in s.exec(select(M.Book).where(M.Book.store_id == store_id)).all():
        s.delete(b)
    for inv in s.exec(select(M.Inventory).where(M.Inventory.store_id == store_id)).all():
        s.delete(inv)
    n = 0
    for i, raw in enumerate(reader, start=1):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        title = row.get("title", "")
        if not title:
            continue
        try:
            price = int(float(row.get("price_rupees") or row.get("price") or 0) * 100)
        except Exception:
            price = 0
        try:
            qty = int(float(row.get("quantity") or 0))
        except Exception:
            qty = 0
        bid = f"{store_id}_u{i}"
        s.add(M.Book(id=bid, store_id=store_id, title=title, author=row.get("author", ""),
                     genre=row.get("genre", ""), price=price, age=(row.get("age") or "adult"),
                     tags=row.get("tags", ""), shelf=row.get("shelf", ""), aisle=row.get("aisle", "")))
        s.add(M.Inventory(store_id=store_id, book_id=bid, quantity=qty))
        n += 1
    s.commit()
    return {"ok": True, "imported": n}

@app.post("/api/v1/stores/{store_id}/books/{book_id}/synopsis")
def book_synopsis(store_id: str, book_id: str, s: Session = Depends(get_session)):
    b = s.get(M.Book, book_id)
    if not b or b.store_id != store_id:
        raise HTTPException(404, "book not found")
    return {"book_id": book_id, "title": b.title, "synopsis": synopsis(book_dict(s, b), GROQ_KEY)}

@app.get("/api/v1/stores/{store_id}/inventory/{book_id}/stock")
def check_stock(store_id: str, book_id: str, s: Session = Depends(get_session)):
    b = s.get(M.Book, book_id)
    if not b or b.store_id != store_id:
        raise HTTPException(404, "book not found")
    d = book_dict(s, b)
    return {"book_id": book_id, "in_stock": d["in_stock"], "quantity": d["quantity"],
            "shelf": b.shelf, "aisle": b.aisle}


# ---------- conversation engine ----------
@app.post("/api/v1/stores/{store_id}/sessions", status_code=201)
def start_session(store_id: str, s: Session = Depends(get_session)):
    store = s.get(M.Store, store_id)
    if not store: raise HTTPException(404, "store not found")
    sess = M.ConvSession(id=sid("se"), store_id=store_id)
    s.add(sess); s.commit()
    # Greeting always uses THIS store's name (entered by the owner during setup).
    greeting = f"Welcome to {store.shop_name}! How can I help you find a book today?"
    return {"session_id": sess.id, "greeting": greeting}

class MessageIn(BaseModel):
    text: str

@app.post("/api/v1/stores/{store_id}/sessions/{session_id}/message")
def message(store_id: str, session_id: str, body: MessageIn, s: Session = Depends(get_session)):
    store = s.get(M.Store, store_id)
    shop = store.shop_name if store else "the bookstore"
    st = SESSION_STATE.setdefault(session_id, {"slots": {}, "asked": 0, "age_ok": False})
    text = body.text or ""
    low = text.lower()

    # capture an age confirmation if we were waiting for one
    if st.get("await_age") and re.search(r"\b(yes|yeah|i am|i'm|over 18|18|adult|confirm)\b", low):
        st["age_ok"] = True
        st.pop("await_age", None)

    # HARD RULE: adult/18+ content requires confirmation, else hand off to staff
    if mature_content_requested(text) and not st["age_ok"]:
        st["await_age"] = True
        return {"state": "responding",
                "reply": "That's an adult title — could you confirm you're over 18? If you'd prefer, I'll ask a staff member to help you with that.",
                "recommendations": None, "requires_age_confirmation": True, "requires_staff_handoff": True}

    # The reply to the question we just asked IS the answer to that slot -> record it,
    # so discovery always advances (never loops on the same question).
    if st.get("last_asked"):
        k = st["last_asked"]
        st["slots"][k] = (str(st["slots"].get(k, "")) + " " + text).strip()
        st["last_asked"] = None
    extract_slots(text, st["slots"])

    candidates = [book_dict(s, b) for b in
                  s.exec(select(M.Book).where(M.Book.store_id == store_id)).all()]
    if not candidates:
        raise HTTPException(404, "no catalog for store")
    # HARD RULE: age-appropriate filtering for young readers
    if st["slots"].get("age_band") in ("child", "middle-grade"):
        candidates = child_safe(candidates)

    # SPECIFIC TITLE: if the customer names an exact book we carry, answer about THAT book now.
    # In stock -> show its shelf/aisle. Out of stock -> show it marked SOLD OUT + similar-genre picks.
    req = requested_title_book(text, candidates)
    if req:
        st.setdefault("history", []).append({"role": "user", "content": text})
        sel = specific_title_response(req, candidates, st["slots"])
        top = sel["top_pick"]
        if top.get("sold_out"):
            reply = (f"{req['title']} isn't available right now — but we have a few books of a "
                     f"similar genre you might love.")
        else:
            reply = f"Yes — {req['title']} is on our shelves. Here it is, with a couple you might also enjoy."
        st["history"].append({"role": "assistant", "content": reply})
        for c in [top, *sel["alternatives"]]:
            s.add(M.SalesEvent(store_id=store_id, session_id=session_id,
                               book_id=c["book_id"], event_type="interested"))
        if top.get("sold_out"):   # capture lost demand for the requested title
            row = s.exec(select(M.AskedNotBought).where(M.AskedNotBought.store_id == store_id,
                                                        M.AskedNotBought.title == req["title"])).first()
            if row:
                row.times_asked += 1; s.add(row)
            else:
                s.add(M.AskedNotBought(store_id=store_id, title=req["title"], times_asked=1))
        s.commit()
        return {"state": "responding", "reply": reply,
                "recommendations": {"top_pick": top, "alternatives": sel["alternatives"]},
                "sold_out": bool(top.get("sold_out")),
                "suggested_action": "create_procurement" if top.get("sold_out") else "add_to_basket",
                "requires_staff_handoff": False}

    # LLM-DRIVEN natural conversation (Groq/Gemini). Cards stay grounded in real stock.
    st.setdefault("history", [])
    st["history"].append({"role": "user", "content": text})
    user_turns = sum(1 for m in st["history"] if m["role"] == "user")
    conv = converse(st["history"], candidates, GROQ_KEY, shop, turn=user_turns)
    if conv is not None:
        reply, query = conv   # converse() keeps profiling naturally until it knows the reader
        st["history"].append({"role": "assistant", "content": reply})
        if query:
            sel = select_books(query, candidates, st["slots"])
            top = sel["top_pick"]
            for c in [top, *sel["alternatives"]]:
                s.add(M.SalesEvent(store_id=store_id, session_id=session_id,
                                   book_id=c["book_id"], event_type="interested"))
            if not top["in_stock"]:
                row = s.exec(select(M.AskedNotBought).where(M.AskedNotBought.store_id == store_id,
                                                            M.AskedNotBought.title == top["title"])).first()
                if row:
                    row.times_asked += 1; s.add(row)
                else:
                    s.add(M.AskedNotBought(store_id=store_id, title=top["title"], times_asked=1))
            s.commit()
            return {"state": "responding", "reply": reply,
                    "recommendations": {"top_pick": top, "alternatives": sel["alternatives"]},
                    "suggested_action": "add_to_basket" if top["in_stock"] else "create_procurement",
                    "requires_staff_handoff": False}
        return {"state": "responding", "reply": reply, "recommendations": None,
                "requires_staff_handoff": False}

    # ---- Rule-based fallback (only when NO LLM key is set) ----
    # DISCOVERY: ask up to 5 short questions, skipping slots already known
    if not should_recommend(text, st["slots"], st["asked"]):
        key, q = next_question(st["slots"])
        if q:
            st["asked"] += 1
            st["last_asked"] = key   # remember which slot this question was for
            return {"state": "responding", "reply": q, "recommendations": None,
                    "slots_filled": st["slots"], "requires_staff_handoff": False}

    result = recommend(text, candidates, st["slots"], GROQ_KEY, shop)
    for c in [result["top_pick"], *result["alternatives"]]:
        s.add(M.SalesEvent(store_id=store_id, session_id=session_id,
                           book_id=c["book_id"], event_type="interested"))
    top = result["top_pick"]
    if not top["in_stock"]:  # capture lost demand
        row = s.exec(select(M.AskedNotBought).where(M.AskedNotBought.store_id == store_id,
                                                    M.AskedNotBought.title == top["title"])).first()
        if row:
            row.times_asked += 1; s.add(row)
        else:
            s.add(M.AskedNotBought(store_id=store_id, title=top["title"], times_asked=1))
    s.commit()
    return {
        "state": "responding",
        "reply": result["reply"],
        "recommendations": {"top_pick": top, "alternatives": result["alternatives"]},
        "slots_filled": st["slots"],
        "suggested_action": "add_to_basket" if top["in_stock"] else "create_procurement",
        "requires_staff_handoff": False,
    }


# ---------- basket ----------
class BasketIn(BaseModel):
    book_id: str
    quantity: int = 1

def basket_payload(s, store_id, session_id):
    items = s.exec(select(M.BasketItem).where(M.BasketItem.session_id == session_id)).all()
    out, total = [], 0
    for it in items:
        b = s.get(M.Book, it.book_id)
        if b:
            total += b.price * it.quantity
            out.append({"book_id": b.id, "title": b.title, "author": b.author, "price": b.price,
                        "price_label": rupees(b.price), "quantity": it.quantity})
    return {"items": out, "total": total, "total_label": rupees(total), "currency": "INR"}

@app.post("/api/v1/stores/{store_id}/sessions/{session_id}/basket")
def add_basket(store_id: str, session_id: str, body: BasketIn, s: Session = Depends(get_session)):
    b = s.get(M.Book, body.book_id)
    if not b or b.store_id != store_id:
        raise HTTPException(404, "book not found in this store")
    qty = max(1, body.quantity)
    s.add(M.BasketItem(store_id=store_id, session_id=session_id, book_id=body.book_id, quantity=qty))
    s.add(M.SalesEvent(store_id=store_id, session_id=session_id, book_id=body.book_id, event_type="added_to_basket"))
    s.commit()
    return basket_payload(s, store_id, session_id)

@app.get("/api/v1/stores/{store_id}/sessions/{session_id}/basket")
def get_basket(store_id: str, session_id: str, s: Session = Depends(get_session)):
    return basket_payload(s, store_id, session_id)

@app.delete("/api/v1/stores/{store_id}/sessions/{session_id}/basket/{book_id}")
def remove_basket(store_id: str, session_id: str, book_id: str, s: Session = Depends(get_session)):
    for it in s.exec(select(M.BasketItem).where(M.BasketItem.session_id == session_id,
                                                M.BasketItem.book_id == book_id)).all():
        s.delete(it)
    s.commit()
    return basket_payload(s, store_id, session_id)

@app.post("/api/v1/stores/{store_id}/sessions/{session_id}/checkout")
def checkout(store_id: str, session_id: str, s: Session = Depends(get_session)):
    items = s.exec(select(M.BasketItem).where(M.BasketItem.session_id == session_id)).all()
    for it in items:
        s.add(M.SalesEvent(store_id=store_id, session_id=session_id, book_id=it.book_id,
                           event_type="purchased", quantity=it.quantity))
        inv = s.exec(select(M.Inventory).where(M.Inventory.book_id == it.book_id,
                                               M.Inventory.store_id == store_id)).first()
        if inv:
            inv.quantity = max(0, inv.quantity - it.quantity); s.add(inv)
        s.delete(it)
    s.commit()
    return {"ok": True, "sold": len(items)}


# ---------- procurement ----------
class ProcIn(BaseModel):
    title: str
    book_id: str = ""
    customer_phone: str
    session_id: str = ""

@app.post("/api/v1/stores/{store_id}/procurement", status_code=201)
def create_procurement(store_id: str, body: ProcIn, background: BackgroundTasks,
                       s: Session = Depends(get_session)):
    p = M.Procurement(id=sid("pr"), store_id=store_id, title=body.title, book_id=body.book_id,
                      customer_phone=body.customer_phone)
    s.add(p); s.commit(); s.refresh(p)
    # webhook fires in the background (never blocks the kiosk's confirmation)
    background.add_task(_fire_webhook, {"event": "requested", "store_id": store_id, "title": p.title,
                                        "customer_phone": p.customer_phone, "eta_days": p.eta_days})
    return {"id": p.id, "title": p.title, "status": p.status, "eta_days": p.eta_days,
            "customer_phone_masked": mask(p.customer_phone)}

@app.get("/api/v1/stores/{store_id}/procurement")
def list_procurement(store_id: str, status: Optional[str] = None, s: Session = Depends(get_session)):
    stmt = select(M.Procurement).where(M.Procurement.store_id == store_id)
    if status: stmt = stmt.where(M.Procurement.status == status)
    return [proc_view(p) for p in s.exec(stmt).all()]

class ProcStatusIn(BaseModel):
    status: str

@app.patch("/api/v1/stores/{store_id}/procurement/{proc_id}")
def update_procurement(store_id: str, proc_id: str, body: ProcStatusIn, background: BackgroundTasks,
                       s: Session = Depends(get_session)):
    p = s.get(M.Procurement, proc_id)
    if not p or p.store_id != store_id:
        raise HTTPException(404, "procurement not found")
    old = p.status
    p.status = body.status
    s.add(p); s.commit(); s.refresh(p)
    # fire ONLY on the transition into 'arrived' (guards against double-click => double message)
    if old != "arrived" and p.status == "arrived":
        background.add_task(_fire_webhook, {"event": "arrived", "store_id": store_id, "title": p.title,
                                            "customer_phone": p.customer_phone})
    return proc_view(p)

_log = logging.getLogger("webhook")

def _fire_webhook(payload):
    """Runs in a BackgroundTask (never blocks the API). Authenticated + logged."""
    url = os.getenv("N8N_PROCUREMENT_WEBHOOK")  # unset by default -> no-op, core unaffected
    if not url:
        return
    try:
        import httpx
        headers = {}
        secret = os.getenv("N8N_WEBHOOK_SECRET")
        if secret:
            headers["X-Webhook-Token"] = secret       # n8n verifies via Header Auth credential
        r = httpx.post(url, json=payload, headers=headers, timeout=5)
        if r.status_code >= 300:
            _log.warning("n8n webhook non-2xx: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        _log.warning("n8n webhook failed (%s): %s", payload.get("event"), e)


def proc_view(p):
    """Procurement row with computed eta_date + is_overdue (fixes the W4 blocker)."""
    eta_date = (p.created_at + timedelta(days=p.eta_days)) if p.created_at else None
    is_overdue = bool(eta_date and eta_date < datetime.utcnow() and p.status not in ("closed", "arrived"))
    return {"id": p.id, "title": p.title, "customer_phone_masked": mask(p.customer_phone),
            "status": p.status, "eta_days": p.eta_days,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "eta_date": eta_date.isoformat() if eta_date else None,
            "is_overdue": is_overdue}


# ---------- events ----------
class EventIn(BaseModel):
    session_id: str = ""
    book_id: str = ""
    event_type: str
    quantity: int = 1
    staff_id: str = ""

@app.post("/api/v1/stores/{store_id}/events", status_code=201)
def log_event(store_id: str, body: EventIn, s: Session = Depends(get_session)):
    s.add(M.SalesEvent(store_id=store_id, **body.model_dump())); s.commit()
    return {"ok": True}


# ---------- admin ----------
class PinIn(BaseModel):
    pin: str

@app.post("/api/v1/stores/{store_id}/admin/verify-pin")
def verify_pin(store_id: str, body: PinIn):
    return {"ok": body.pin == ADMIN_PIN}

@app.get("/api/v1/stores/{store_id}/reports/summary")
def report_summary(store_id: str, range: str = "today", s: Session = Depends(get_session)):
    all_evs = s.exec(select(M.SalesEvent).where(M.SalesEvent.store_id == store_id)).all()
    window_days = {"today": 1, "week": 7, "month": 30, "year": 365}.get(range, 3650)
    since = datetime.utcnow() - timedelta(days=window_days)
    evs = [e for e in all_evs if (e.created_at or datetime.utcnow()) >= since]

    interested = len({e.session_id for e in evs if e.event_type == "interested"})
    basket = len([e for e in evs if e.event_type == "added_to_basket"])
    purchased = len([e for e in evs if e.event_type == "purchased"])
    customers = max(len({e.session_id for e in evs}), interested)
    tally = {}
    for e in evs:
        if e.event_type in ("purchased", "added_to_basket"):
            b = s.get(M.Book, e.book_id)
            if b:
                tally[b.title] = tally.get(b.title, 0) + 1
    top_books = [{"title": t, "units": u} for t, u in sorted(tally.items(), key=lambda x: -x[1])[:5]]
    gtally = {}
    for e in evs:
        b = s.get(M.Book, e.book_id)
        if b:
            gtally[b.genre] = gtally.get(b.genre, 0) + 1
    top_genres = [{"genre": g, "count": c} for g, c in sorted(gtally.items(), key=lambda x: -x[1])]
    proc = [proc_view(p) for p in
            s.exec(select(M.Procurement).where(M.Procurement.store_id == store_id)).all()]
    lost = [{"title": a.title, "times_asked": a.times_asked}
            for a in s.exec(select(M.AskedNotBought).where(M.AskedNotBought.store_id == store_id)).all()]
    lost.sort(key=lambda x: -x["times_asked"])
    conv = round(purchased / customers, 2) if customers else 0
    return {
        "range": range,
        "kpis": {"customers": customers, "purchases": purchased,
                 "conversion_rate": conv, "basket_events": basket},
        "funnel": {"interested": interested or customers, "added_to_basket": basket, "purchased": purchased},
        "top_books": top_books, "top_genres": top_genres,
        "procurement": proc, "lost_demand": lost,
    }
