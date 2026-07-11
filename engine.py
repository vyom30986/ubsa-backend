"""Conversation engine for the in-store AI bookseller.

Encodes the behavioural spec:
  - Voice-first, warm, concise, persuasive-not-pushy bookseller.
  - Slot-based discovery (max 5 questions), skipping slots already known.
  - Title-first recommendations: 1 Top Pick + 2 Alternatives (safer / more adventurous),
    each with a hook, a "why it suits you", and an "If you liked X..." reference.
  - HARD RULES enforced in code (not left to the model):
      * Stock is only ever read from the DB (this module never invents availability).
      * Never mention online marketplaces, audiobooks, or digital formats.
      * Never prompt for gift wrap.
      * Under-13 -> age-appropriate filtering.
      * Adult/18+ -> requires explicit age confirmation, else hand off to staff.
"""
import os
import re
import random

# never say these
FORBIDDEN = ("kindle", "ebook", "e-book", "audiobook", "audible", "amazon",
             "flipkart", "online", "download", "gift wrap", "gift-wrap")

MATURE_MARKERS = ("erotica", "erotic", "explicit", "18+", "adult content", "nsfw", "smut")

# The system prompt handed to the LLM (Groq) for phrasing. Shop name is filled in.
SYSTEM_PROMPT = """You are the in-store AI bookseller for {shop}, a physical bookstore. You talk to walk-in customers on a tablet, voice-first.

IDENTITY & TONE
- Warm, knowledgeable, curious, concise; persuasive but never pushy - like an excellent human bookseller.
- Adapt tone: for children, be gentle, playful and fun; for adults, be crisp and insightful.
- Spoken replies must be SHORT: about 30-35 words (~12 seconds). Break longer info into follow-ups.

RECOMMENDING
- Always TITLE-FIRST: say the book's title before you explain anything.
- Give exactly one Top Pick, then two alternatives (one safer, one more adventurous).
- For each: a one-line hook, why it suits THIS reader, and an "If you liked X..." reference.
- Prefer in-stock titles. Stock always comes from the store system - never guess or state stock yourself.
- If a wanted book is out of stock, say exactly: "We can arrange this in 2-3 days. May I take your number to confirm?"
- You may cross-sell ONLY physical in-store items: sequels, box sets, special/collector editions, journals, bookmarks, merchandise.

HARD RULES (never break)
- NEVER mention online marketplaces, e-commerce, Kindle, e-books, audiobooks or any digital format. Physical books only.
- NEVER prompt for gift wrap.
- For under-13 readers, only suggest age-appropriate books; keep it gentle and fun.
- For adult/18+ content, require explicit age confirmation first; if it isn't given, don't share it - hand off to staff.
- Mask personal details in speech (e.g. "the number ending 4321"). Full details live only in the store log.

CLOSING
- Use micro-closes: "Shall I add this to your basket?" / "Standard hardcover or special edition?"
- End by summarising the customer's choices and offering next steps: add to basket, hear a synopsis, or see alternatives.
"""

# Per-title hook + "if you liked" anchor (falls back to generated text for unknown books).
META = {
    "The Lightning Thief":       ("A modern myth-adventure - an ordinary boy learns he's the son of a god.", "Harry Potter"),
    "The Wildwood Chronicles":   ("A wild, whimsical quest into a forbidden magical forest.", "The Chronicles of Narnia"),
    "Matilda":                   ("A tiny genius versus awful grown-ups - funny and big-hearted.", "Charlie and the Chocolate Factory"),
    "The Hobbit":                ("A cosy-then-epic quest with dragons and a reluctant hero.", "The Chronicles of Narnia"),
    "Atomic Habits":             ("Tiny habit tweaks that compound into big change.", "The Power of Habit"),
    "Deep Work":                 ("How to focus deeply in a distracted world.", "Atomic Habits"),
    "The Alchemist":             ("A shepherd's journey to his dream - a gentle modern fable.", "The Little Prince"),
    "Ikigai":                    ("The Japanese secret to a calm, purposeful life.", "The Monk Who Sold His Ferrari"),
    "Wings of Fire":             ("Kalam's inspiring rise from a small town to the stars.", "The Diary of a Young Girl"),
    "Godaan":                    ("Premchand's timeless classic of village life and dignity.", "Gaban"),
    "Gaban":                     ("An easy, moving Premchand story to begin with.", "Godaan"),
    "Sapiens":                   ("A sweeping story of how humans came to rule the world.", "Guns, Germs, and Steel"),
}

# ---------------- Discovery slots ----------------

SLOT_QUESTIONS = {
    "interests":     "To point you right - what kind of stories or subjects do they enjoy? A favourite genre, author, or mood is perfect.",
    "age_band":      "Lovely - and who is the book for? A child, a teen, or an adult reader?",
    "reading_level": "Got it. Are they a brand-new reader, an occasional one, or a very keen reader?",
    "context":       "Is this for them or a gift - and is there any occasion or hurry?",
    "constraints":   "Anything to keep in mind - a budget, a language, or a length you'd prefer?",
}
SLOT_ORDER = ["interests", "age_band", "reading_level", "context", "constraints"]

GENRE_WORDS = ["fantasy", "adventure", "mystery", "romance", "thriller", "history",
               "science", "self help", "self-help", "productivity", "biography",
               "poetry", "hindi", "regional", "children", "classic", "fiction",
               "nonfiction", "horror", "comic", "philosophy"]


def extract_slots(text, slots):
    """Fill any slots this utterance reveals. Existing slots are kept."""
    t = text.lower()
    m = re.search(r"\b(\d{1,2})\s*(?:year|yr|yo|-year)", t)
    age_num = int(m.group(1)) if m else None
    if re.search(r"\b(child|kid|kids|son|daughter|little one|toddler)\b", t) or (age_num is not None and age_num <= 8):
        slots["age_band"] = "child"
    elif re.search(r"middle.?grade", t) or (age_num is not None and 9 <= age_num <= 12):
        slots["age_band"] = "middle-grade"
    elif re.search(r"\bteen|young adult|\bya\b\b", t) or (age_num is not None and 13 <= age_num <= 17):
        slots["age_band"] = "ya"
    elif re.search(r"\b(adult|myself|for me|husband|wife|colleague|manager|grown-?up)\b", t):
        slots.setdefault("age_band", "adult")

    if re.search(r"beginner|new reader|just start|reluctant|first book", t):
        slots["reading_level"] = "beginner"
    elif re.search(r"avid|voracious|loves reading|reads a lot|keen reader", t):
        slots["reading_level"] = "avid"
    elif re.search(r"occasional|now and then|sometimes reads", t):
        slots["reading_level"] = "occasional"

    if any(g in t for g in GENRE_WORDS) or re.search(r"\blike|love|enjoy|similar to|funny|dark|inspiring|calm|cosy|cozy|gripping\b", t):
        slots["interests"] = (slots.get("interests", "") + " " + text).strip()

    if re.search(r"budget|under \d|cheap|price|short|long|pages|hindi|english|language|sensitiv", t):
        slots["constraints"] = (slots.get("constraints", "") + " " + text).strip()

    if re.search(r"\bgift|present|birthday|for my|for a friend|for her|for him\b", t):
        slots["context"] = "gift"
    elif re.search(r"\bfor me|myself\b", t):
        slots.setdefault("context", "self")
    if re.search(r"urgent|today|right now|in a hurry|leaving", t):
        slots["urgency"] = "urgent"
    return slots


def wants_direct_recommendation(text):
    return bool(re.search(r"surprise me|just recommend|anything|suggest|pick for me|you choose|recommend something", text.lower()))


def should_recommend(text, slots, asked):
    """Recommend as soon as we know what they like (common case), or after enough turns."""
    if wants_direct_recommendation(text):
        return True
    if slots.get("interests"):
        return True
    if asked >= 4:               # spec: at most 5 questions
        return True
    return False


def next_question(slots):
    for k in SLOT_ORDER:
        if not slots.get(k):
            return k, SLOT_QUESTIONS[k]
    return None, None


# ---------------- Age safety ----------------

def mature_content_requested(text):
    t = text.lower()
    return any(m in t for m in MATURE_MARKERS)


def child_safe(candidates):
    """Keep only age-appropriate books for under-13 readers."""
    out = [b for b in candidates if b.get("age", "").lower() not in ("adult", "18+", "mature")]
    return out or candidates


# ---------------- Recommendation ----------------

def score_books(text, slots, candidates):
    blob = (text + " " + slots.get("interests", "")).lower()
    tokens = [w for w in re.split(r"\W+", blob) if len(w) > 2]
    genre_bias = None
    if re.search(r"manager|business|work|productiv|habit|leadership", blob):
        genre_bias = "Self-help"
    if re.search(r"hindi|regional|premchand", blob):
        genre_bias = "Regional"
    if slots.get("age_band") in ("child", "middle-grade"):
        genre_bias = "Children"

    scored = []
    for b in candidates:
        hay = f"{b.get('tags','')} {b.get('genre','')} {b.get('title','')} {b.get('author','')}".lower()
        s = sum(2 for w in tokens if w in hay)
        if genre_bias and b.get("genre") == genre_bias:
            s += 5
        if b.get("in_stock"):
            s += 1  # gentle preference for in-stock
        s += random.random() * 0.3
        scored.append((s, b))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [b for _, b in scored]


def _meta(title):
    if title in META:
        return META[title]
    return ("A great pick that fits what you're after.", "")


def _location(b):
    """Green shelf/aisle string for in-stock books, e.g. 'Shelf 3 · Aisle B'."""
    parts = []
    if b.get("shelf"): parts.append(f"Shelf {b['shelf']}")
    if b.get("aisle"): parts.append(f"Aisle {b['aisle']}")
    return " · ".join(parts) if parts else "In stock"


def decorate(b, label, slots):
    hook, anchor = _meta(b["title"])
    who = {"child": "a young reader", "middle-grade": "a middle-grade reader",
           "ya": "a teen reader", "adult": "an adult reader"}.get(slots.get("age_band"), "this reader")
    return {
        "book_id": b["id"], "title": b["title"], "author": b["author"], "genre": b["genre"],
        "price": b["price"], "price_label": b["price_label"],
        "hook": hook,
        "why": f"A strong match for {who} who enjoys {b['genre'].lower()}.",
        "if_you_liked": anchor,
        "label": label,
        "in_stock": b["in_stock"], "shelf": b["shelf"], "aisle": b["aisle"],
        "sold_out": False,
        "availability": _location(b) if b["in_stock"] else "Arrives in 2-3 days",
    }


def requested_title_book(text, candidates):
    """If the customer named a specific catalogue title in this message, return that book (dict), else None."""
    t = (text or "").lower()
    best = None
    for b in candidates:
        title = (b.get("title") or "").lower()
        if len(title) >= 4 and title in t:
            if best is None or len(title) > len((best.get("title") or "")):
                best = b
    return best


def specific_title_response(book, candidates, slots=None):
    """Customer asked for ONE specific book. Show it as the top card (marked sold-out if
    unavailable) plus two same-genre alternatives that ARE in stock where possible."""
    slots = slots or {}
    genre = book.get("genre")
    same = [b for b in candidates if b.get("genre") == genre and b["id"] != book["id"]]
    same_instock = [b for b in same if b.get("in_stock")]
    others_instock = [b for b in candidates if b["id"] != book["id"] and b.get("in_stock") and b not in same]
    pool = same_instock + others_instock + [b for b in same if not b.get("in_stock")]
    if not pool:
        pool = [b for b in candidates if b["id"] != book["id"]]
    alts = pool[:2]
    top = decorate(book, "top", slots)
    if not book.get("in_stock"):
        top["sold_out"] = True
        top["availability"] = "Sold out"
    return {"top_pick": top, "alternatives": [decorate(a, "safer", slots) for a in alts]}


def pick_three(ranked):
    top = ranked[0]
    rest = ranked[1:] or [top]
    safer = next((b for b in rest if b.get("in_stock")), rest[0])
    adventurous = next((b for b in rest if b["genre"] != top["genre"] and b is not safer), None)
    if adventurous is None:
        adventurous = next((b for b in rest if b is not safer), rest[-1])
    return top, safer, adventurous


def phrase_reply(top, groq_key, slots, shop="the bookstore"):
    """Short, title-first, spoken-length reply. Uses Groq if available, else a warm template."""
    hook, anchor = _meta(top["title"])
    # Prefer Gemini if a key is set (free tier), then Groq, then a template.
    gem = os.getenv("GEMINI_API_KEY")
    if gem:
        try:
            import httpx
            prompt = (SYSTEM_PROMPT.format(shop=shop) +
                      f'\nCustomer profile: {slots}. Recommend the top pick "{top["title"]}" by '
                      f'{top["author"]} ({top["genre"]}). Hook: {hook}. Similar to: {anchor or "n/a"}. '
                      f'Write ONE short spoken sentence, title first, warm and concise.')
            r = httpx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
                params={"key": gem},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=20)
            r.raise_for_status()
            out = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if out and not any(f in out.lower() for f in FORBIDDEN):
                return out
        except Exception:
            pass
    if groq_key:
        try:
            import httpx
            user = (f'Customer profile: {slots}. Top pick to recommend: "{top["title"]}" by {top["author"]} '
                    f'({top["genre"]}). Hook: {hook}. Similar to: {anchor or "n/a"}. '
                    f'Write ONE short spoken sentence, title first, warm and concise.')
            model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
            payload = {"model": model,
                       "messages": [{"role": "system", "content": SYSTEM_PROMPT.format(shop=shop)},
                                    {"role": "user", "content": user}],
                       "max_tokens": 120, "temperature": 0.7}
            if model.startswith("openai/gpt-oss"):
                payload["reasoning_effort"] = "low"
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                           headers={"Authorization": f"Bearer {groq_key}"}, json=payload, timeout=20)
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"].strip()
            if out and not any(f in out.lower() for f in FORBIDDEN):
                return out
        except Exception:
            pass
    # Template fallback - title first, with the "if you liked" hook
    lead = f"<b>{top['title']}</b> — {hook}"
    if anchor:
        lead += f" If you liked {anchor}, you'll love this."
    return lead


def recommend(text, candidates, slots=None, groq_key=None, shop="the bookstore"):
    slots = slots or {}
    ranked = score_books(text, slots, candidates)
    while len(ranked) < 3 and candidates:
        ranked.append(candidates[len(ranked) % len(candidates)])
    top, safer, adventurous = pick_three(ranked)
    return {
        "reply": phrase_reply(top, groq_key, slots, shop),
        "top_pick": decorate(top, "top", slots),
        "alternatives": [decorate(safer, "safer", slots), decorate(adventurous, "adventurous", slots)],
    }


def select_books(query, candidates, slots=None):
    """Pick Top Pick + safer + adventurous from the live catalogue (no phrasing)."""
    slots = slots or {}
    ranked = score_books(query, slots, candidates)
    while len(ranked) < 3 and candidates:
        ranked.append(candidates[len(ranked) % len(candidates)])
    top, safer, adventurous = pick_three(ranked)
    return {"top_pick": decorate(top, "top", slots),
            "alternatives": [decorate(safer, "safer", slots), decorate(adventurous, "adventurous", slots)]}


def _llm_chat(messages, groq_key):
    """One chat completion. Uses Gemini if GEMINI_API_KEY set, else Groq. Returns text or None."""
    gem = os.getenv("GEMINI_API_KEY")
    try:
        import httpx
        if gem:
            sys_txt = " ".join(m["content"] for m in messages if m["role"] == "system")
            contents = [{"role": "model" if m["role"] == "assistant" else "user",
                         "parts": [{"text": m["content"]}]} for m in messages if m["role"] != "system"]
            body = {"contents": contents}
            if sys_txt:
                body["systemInstruction"] = {"parts": [{"text": sys_txt}]}
            r = httpx.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
                           params={"key": gem}, json=body, timeout=20)
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        gk = groq_key or os.getenv("GROQ_API_KEY")
        if gk:
            model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
            payload = {"model": model, "messages": messages, "max_tokens": 260, "temperature": 0.7}
            if model.startswith("openai/gpt-oss"):
                payload["reasoning_effort"] = "low"
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                           headers={"Authorization": f"Bearer {gk}"}, json=payload, timeout=20)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None
    return None


def converse(history, candidates, groq_key, shop="the bookstore", turn=0):
    """LLM-driven natural conversation. Returns (reply_text, recommend_query_or_None),
    or None if no LLM key is configured (caller then uses the rule-based flow)."""
    if not (groq_key or os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")):
        return None
    cat = "; ".join(f"{b['title']} by {b['author']} ({b['genre']}, "
                    f"{'in stock' if b['in_stock'] else 'out of stock'})" for b in candidates[:40])
    sys = SYSTEM_PROMPT.format(shop=shop) + (
        "\n\nYou may ONLY recommend from this catalogue: " + cat +
        "\n\nYou are having a CANDID, human conversation to understand the reader before recommending. Do NOT sell early. "
        "ALWAYS build on what the customer just said - never ask something they already answered. "
        "Move the profile forward ONE natural question at a time. For example, if they name a favourite (like Harry Potter) "
        "but you don't yet know the READER'S AGE, ask their age group next; then reading level, favourite themes, or the occasion. "
        "Ask about three short questions across the chat, one at a time, letting them answer each, BEFORE recommending anything. "
        + f"So far the customer has sent {turn} message(s); if that is fewer than 3, ask ONE more natural question that follows on from what they said. "
        + "Only once you truly understand the reader, write ONE warm sentence and END your reply with a line exactly like "
        "[[RECOMMEND: <a few keywords>]] - the app then shows the book cards, so do NOT list titles yourself. "
        "IMPORTANT: If the customer asks for a SPECIFIC book BY TITLE (for example 'Harry Potter') that is NOT in the "
        "catalogue above, do NOT pretend to have it and do NOT keep asking questions - instead END your reply with a line "
        "exactly like [[UNAVAILABLE: <the exact title they asked for> || <2-4 genre or theme keywords for close matches>]]. "
        "The app will then apologise by name and show the closest in-stock books. "
        "While still getting to know them, ask ONE short question and do NOT include any tag."
    )
    messages = [{"role": "system", "content": sys}] + history[-10:]
    out = _llm_chat(messages, groq_key)
    if not out:
        return None

    def parse(txt):
        un = re.search(r"\[\[\s*UNAVAILABLE:(.*?)\]\]", txt, re.S)
        unavailable = un_kw = None
        if un:
            inner = un.group(1).strip()
            if "||" in inner:
                unavailable, un_kw = [p.strip() for p in inner.split("||", 1)]
            else:
                unavailable = inner
        mm = re.search(r"\[\[\s*RECOMMEND:(.*?)\]\]", txt, re.S)
        q = mm.group(1).strip() if mm else None
        rp = re.sub(r"\[\[\s*(?:RECOMMEND|UNAVAILABLE):.*?\]\]", "", txt, flags=re.S).strip()
        return rp, q, unavailable, un_kw

    reply, query, unavailable, un_kw = parse(out)
    # Customer named a specific title we do NOT carry -> apologise + show closest matches now.
    if unavailable:
        return {"reply": reply, "query": (un_kw or query or unavailable), "unavailable": unavailable}
    # Too early to recommend -> ask the LLM for one MORE natural, contextual question
    # (not a canned one), so it follows on from what the customer just said.
    if query and turn < 3:
        nudge = messages + [
            {"role": "assistant", "content": out},
            {"role": "user", "content": "(Please don't recommend a book yet. Ask ONE more short, natural question that "
                                        "follows on from what I just told you - for instance the reader's age group or "
                                        "reading level. No book titles, no RECOMMEND tag.)"}]
        out2 = _llm_chat(nudge, groq_key)
        if out2:
            reply, _, _, _ = parse(out2)
        query = None

    if any(f in reply.lower() for f in FORBIDDEN):
        reply = "Let me find you a lovely physical copy from our shelves."
    if not reply:
        reply = "Could you tell me a little more about the reader?"
    return {"reply": reply, "query": query, "unavailable": None}


def synopsis(book, groq_key):
    """Spoiler-free ~3-4 sentence summary of a book. LLM if available, else a warm template."""
    title = book.get("title", ""); author = book.get("author", "")
    genre = book.get("genre", ""); tags = book.get("tags", "")
    hook, anchor = _meta(title)
    if groq_key or os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY"):
        try:
            msgs = [
                {"role": "system", "content": "You are a warm in-store bookseller. Give a SPOILER-FREE summary of the "
                 "book in 3-4 short sentences: its premise, themes and mood, and who might love it. Never reveal the "
                 "ending, climax or any twist. Never mention Kindle, e-books, audiobooks or online stores."},
                {"role": "user", "content": f'Book: "{title}" by {author} ({genre}). Themes/keywords: {tags}. '
                 f'Reminds readers of: {anchor or "n/a"}.'}]
            out = _llm_chat(msgs, groq_key)
            if out and not any(f in out.lower() for f in FORBIDDEN):
                return out.strip()
        except Exception:
            pass
    base = f"{title} by {author} is a {genre.lower()} read. {hook}"
    if anchor:
        base += f" If you enjoyed {anchor}, this has a similar spirit."
    base += " I won't spoil where it goes — but it's a wonderful journey."
    return base
