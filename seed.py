"""Seeds one demo store + catalog + inventory + historical sales so the API and
reports work immediately. Replace with a real CSV import (import_catalog) for a live shop."""
import csv
import random
from datetime import datetime, timedelta
from sqlmodel import Session, select
from db import engine
import models as M

DEMO_STORE = "st_demo"

CATALOG = [
    # id, title, author, genre, price(paise), age, tags, shelf, aisle, qty
    ("b1","The Lightning Thief","Rick Riordan","Children",35000,"9-12","adventure fantasy hero gods percy jackson magic quest middle grade","A4","2",4),
    ("b2","The Wildwood Chronicles","Colin Meloy","Children",39900,"8-12","adventure forest magic children fantasy journey","A5","2",2),
    ("b3","Matilda","Roald Dahl","Children",29900,"7-10","children funny school clever girl gentle classic kids","A2","2",6),
    ("b4","The Hobbit","J.R.R. Tolkien","Fiction",49900,"12+","adventure fantasy classic journey dragon quest epic","C1","4",3),
    ("b5","Atomic Habits","James Clear","Self-help",55000,"adult","habits productivity self help manager leadership work discipline business","D3","5",5),
    ("b6","Deep Work","Cal Newport","Self-help",49900,"adult","focus productivity work manager career discipline attention business","D3","5",0),
    ("b7","The Alchemist","Paulo Coelho","Fiction",35000,"adult","inspiring classic journey dreams philosophy gentle fable life","C2","4",7),
    ("b8","Ikigai","Hector Garcia","Self-help",39900,"adult","purpose calm life self help japanese wellbeing meaning","D1","5",4),
    ("b9","Wings of Fire","A.P.J. Abdul Kalam","Regional",29900,"teen","inspiring biography india leadership dreams students teen motivation","E1","6",5),
    ("b10","Godaan","Munshi Premchand","Regional",30000,"adult","hindi regional classic village literature india fiction","E2","6",0),
    ("b11","Gaban","Munshi Premchand","Regional",25000,"adult","hindi regional easy classic literature india fiction story","E2","6",3),
    ("b12","Sapiens","Yuval Noah Harari","Fiction",69900,"adult","history thoughtful nonfiction humanity big ideas classic adult","D5","5",2),
]


def seed_demo():
    with Session(engine) as s:
        existing = s.get(M.Store, DEMO_STORE)
        if existing:
            # correct older databases in place (no need to delete bookstore.db)
            existing.shop_name = "The Reading Room"
            existing.greeting = "Welcome to The Reading Room! What are you in the mood to read today?"
            s.add(existing); s.commit()
            return
        s.add(M.Store(id=DEMO_STORE, shop_name="The Reading Room", owner_name="Riya",
                      city="Pune", greeting="Welcome to The Reading Room! What are you in the mood to read today?",
                      genres_stocked="Fiction,Children,Self-help,Regional"))
        for (bid, title, author, genre, price, age, tags, shelf, aisle, qty) in CATALOG:
            s.add(M.Book(id=bid, store_id=DEMO_STORE, title=title, author=author, genre=genre,
                         price=price, age=age, tags=tags, shelf=shelf, aisle=aisle))
            s.add(M.Inventory(store_id=DEMO_STORE, book_id=bid, quantity=qty))
        s.add(M.Procurement(id="pr_seed1", store_id=DEMO_STORE, title="Deep Work",
                            customer_phone="9876543312", status="ordered", eta_days=2))
        s.commit()


def seed_history():
    """Adds historical sales events so week/month/year reports show real, growing
    numbers. Idempotent: only runs if no old events already exist."""
    with Session(engine) as s:
        existing = s.exec(select(M.SalesEvent).where(M.SalesEvent.store_id == DEMO_STORE)).all()
        old = [e for e in existing if e.created_at and e.created_at < datetime.utcnow() - timedelta(days=2)]
        if old:
            return  # history already seeded
        book_ids = [b[0] for b in CATALOG]
        now = datetime.utcnow()

        def add_events(count, dmin, dmax):
            for _ in range(count):
                ts = now - timedelta(days=random.uniform(dmin, dmax), hours=random.uniform(0, 23))
                bid = random.choice(book_ids)
                sess = "hist_" + str(random.randint(1, 999999))
                s.add(M.SalesEvent(store_id=DEMO_STORE, session_id=sess, book_id=bid,
                                   event_type="interested", created_at=ts))
                if random.random() < 0.62:
                    s.add(M.SalesEvent(store_id=DEMO_STORE, session_id=sess, book_id=bid,
                                       event_type="added_to_basket", created_at=ts))
                if random.random() < 0.44:
                    s.add(M.SalesEvent(store_id=DEMO_STORE, session_id=sess, book_id=bid,
                                       event_type="purchased", created_at=ts))

        add_events(7, 0, 1)       # today
        add_events(26, 1, 7)      # this week
        add_events(70, 7, 30)     # this month
        add_events(210, 30, 350)  # this year

        for title, cnt in [("The Silent Patient", 11), ("Rich Dad Poor Dad", 8),
                           ("It Ends With Us", 6), ("Ikigai", 4)]:
            has = s.exec(select(M.AskedNotBought).where(M.AskedNotBought.store_id == DEMO_STORE,
                                                        M.AskedNotBought.title == title)).first()
            if not has:
                s.add(M.AskedNotBought(store_id=DEMO_STORE, title=title, times_asked=cnt))
        s.commit()


def seed_catalog(store_id):
    """Give a newly-created store the starter catalog so the kiosk works immediately.
    Replace with a real CSV import (import_catalog) for a live shop."""
    with Session(engine) as s:
        if s.exec(select(M.Book).where(M.Book.store_id == store_id)).first():
            return  # already has books
        for (bid, title, author, genre, price, age, tags, shelf, aisle, qty) in CATALOG:
            nid = f"{store_id}_{bid}"
            s.add(M.Book(id=nid, store_id=store_id, title=title, author=author, genre=genre,
                         price=price, age=age, tags=tags, shelf=shelf, aisle=aisle))
            s.add(M.Inventory(store_id=store_id, book_id=nid, quantity=qty))
        s.commit()


def import_catalog(store_id, csv_path):
    """CSV columns: title,author,genre,price_rupees,age,tags,shelf,aisle,quantity"""
    with Session(engine) as s, open(csv_path, newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f), start=1):
            bid = f"{store_id}_b{i}"
            s.add(M.Book(id=bid, store_id=store_id, title=row["title"], author=row.get("author", ""),
                         genre=row.get("genre", ""), price=int(float(row.get("price_rupees", 0)) * 100),
                         age=row.get("age", "adult"), tags=row.get("tags", ""),
                         shelf=row.get("shelf", ""), aisle=row.get("aisle", "")))
            s.add(M.Inventory(store_id=store_id, book_id=bid, quantity=int(row.get("quantity", 0))))
        s.commit()
