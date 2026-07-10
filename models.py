"""Database tables. Multi-tenant from day one: every row carries store_id."""
from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field


class Store(SQLModel, table=True):
    id: str = Field(primary_key=True)
    shop_name: str
    owner_name: str = "Owner"
    city: str = ""
    business_type: str = "independent_bookstore"
    currency: str = "INR"
    greeting: str = ""
    accent_color: str = "#9A6A45"
    languages: str = "en,hi"        # csv
    genres_stocked: str = ""        # csv


class Book(SQLModel, table=True):
    id: str = Field(primary_key=True)
    store_id: str = Field(index=True)
    title: str
    author: str = ""
    genre: str = ""
    price: int = 0                  # paise
    age: str = "adult"
    description: str = ""
    tags: str = ""                  # space/csv keywords for matching
    shelf: str = ""
    aisle: str = ""


class Inventory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    store_id: str = Field(index=True)
    book_id: str = Field(index=True)
    quantity: int = 0


class ConvSession(SQLModel, table=True):
    id: str = Field(primary_key=True)
    store_id: str = Field(index=True)
    language: str = "en"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SalesEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    store_id: str = Field(index=True)
    session_id: str = ""
    book_id: str = ""
    event_type: str = "interested"   # interested | added_to_basket | purchased
    quantity: int = 1
    staff_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BasketItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    store_id: str = Field(index=True)
    session_id: str = Field(index=True)
    book_id: str = ""
    quantity: int = 1


class Procurement(SQLModel, table=True):
    id: str = Field(primary_key=True)
    store_id: str = Field(index=True)
    title: str = ""
    book_id: str = ""
    customer_phone: str = ""         # FULL number — only ever exposed masked
    status: str = "requested"        # requested | ordered | closed
    eta_days: int = 3
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AskedNotBought(SQLModel, table=True):
    """Lost demand: titles asked about but not purchased."""
    id: Optional[int] = Field(default=None, primary_key=True)
    store_id: str = Field(index=True)
    title: str = ""
    times_asked: int = 1
