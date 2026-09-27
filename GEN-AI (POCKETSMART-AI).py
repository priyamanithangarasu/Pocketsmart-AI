import base64
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jose import jwt, JWTError
from passlib.context import CryptContext
from PIL import Image

try:
    import google.generativeai as genai
except Exception:
    genai = None

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pocketsmart.db")
DB_PATH = DATABASE_URL.replace("sqlite:///", "", 1)
if not os.path.isabs(DB_PATH):
    DB_PATH = str(BASE_DIR / DB_PATH)

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(title="PocketSmart AI", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

PLATFORM_URLS = {
    "Amazon": "https://www.amazon.in/s?k=",
    "Flipkart": "https://www.flipkart.com/search?q=",
    "IKEA": "https://www.ikea.com/in/en/search/?q=",
    "Swiggy": "https://www.swiggy.com/search?query=",
    "Zomato": "https://www.zomato.com/search?q=",
    "OYO": "https://www.oyorooms.com/search?location=",
}

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        planner TEXT NOT NULL,
        request_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    conn.commit()
    conn.close()

@app.on_event("startup")
def startup():
    init_db()
    if GEMINI_API_KEY and genai:
        genai.configure(api_key=GEMINI_API_KEY)

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)

def create_token(user_id: int):
    payload = {
        "sub": str(user_id),
        "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        return None
    conn = db()
    user = conn.execute("SELECT id,name,email FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(user) if user else None

def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user

def money(v):
    try:
        return round(float(v), 2)
    except Exception:
        return 0.0

def make_link(platform, query):
    base = PLATFORM_URLS.get(platform, "https://www.google.com/search?q=")
    return base + query.strip().replace(" ", "+") 

def fallback_home(data):
    budget = money(data["budget"])
    rooms = data.get("rooms", "Living Room")
    style = data.get("style", "Modern")
    items = [
        ("Ceiling Light", "Amazon", budget * 0.10),
        ("Ceiling Fan", "Amazon", budget * 0.12),
        ("Storage / Side Table", "IKEA", budget * 0.20),
        ("Wall Decor", "Flipkart", budget * 0.08),
        ("Rug", "IKEA", budget * 0.18),
        ("Curtains", "Amazon", budget * 0.12),
    ]
    return {
        "title": "Home Interior Recommendations",
        "summary": f"A {style} setup for {rooms}, planned within ₹{budget:,.0f}.",
        "budget": budget,
        "allocation": [
            {"category": "Furniture", "amount": round(budget*.38,2)},
            {"category": "Lighting", "amount": round(budget*.22,2)},
            {"category": "Decor", "amount": round(budget*.20,2)},
            {"category": "Soft furnishings", "amount": round(budget*.20,2)},
        ],
        "recommendations": [
            {"name": n, "platform": p, "estimated_price": round(x), "reason": f"Budget-conscious {style.lower()} option for {rooms}.", "url": make_link(p,n)}
            for n,p,x in items
        ]
    }

def fallback_party(data):
    budget = money(data["budget"])
    guests = int(data.get("guests", 10))
    event = data.get("event_type", "Birthday")
    return {
        "title": "Party Budget Plan",
        "summary": f"{event} plan for {guests} guests within ₹{budget:,.0f}.",
        "budget": budget,
        "allocation": [
            {"category": "Catering", "amount": round(budget*.45,2)},
            {"category": "Decoration", "amount": round(budget*.20,2)},
            {"category": "Venue", "amount": round(budget*.20,2)},
            {"category": "Entertainment", "amount": round(budget*.15,2)},
        ],
        "recommendations": [
            {"name": f"Catering for {guests} guests", "platform": "Swiggy", "estimated_price": round(budget*.45), "reason": "Largest allocation for food and beverages.", "url": make_link("Swiggy", f"catering {event}")},
            {"name": f"{event} food options", "platform": "Zomato", "estimated_price": round(budget*.20), "reason": "Compare nearby restaurant and catering choices.", "url": make_link("Zomato", event)},
            {"name": "Budget venue / stay option", "platform": "OYO", "estimated_price": round(budget*.20), "reason": "Useful for venue or accommodation research.", "url": make_link("OYO", data.get("venue", "nearby"))},
            {"name": "Decoration supplies", "platform": "Amazon", "estimated_price": round(budget*.15), "reason": "Flexible DIY decoration option.", "url": make_link("Amazon", f"{event} decoration")},
        ]
    }

def fallback_jewelry(data):
    budget = money(data["budget"])
    occasion = data.get("occasion", "Special Occasion")
    style = data.get("style", "Elegant")
    return {
        "title": "Jewelry Recommendations",
        "summary": f"{style} jewelry ideas for {occasion} within ₹{budget:,.0f}.",
        "budget": budget,
        "allocation": [
            {"category": "Primary jewelry", "amount": round(budget*.60,2)},
            {"category": "Earrings", "amount": round(budget*.20,2)},
            {"category": "Bracelet/Bangle", "amount": round(budget*.12,2)},
            {"category": "Reserve", "amount": round(budget*.08,2)},
        ],
        "recommendations": [
            {"name": f"{style} necklace set", "platform": "Amazon", "estimated_price": round(budget*.55), "reason": f"Suitable for {occasion}.", "url": make_link("Amazon", f"{style} necklace {occasion}")},
            {"name": f"{style} earrings", "platform": "Flipkart", "estimated_price": round(budget*.20), "reason": "Easy way to coordinate the look.", "url": make_link("Flipkart", f"{style} earrings")},
            {"name": f"{style} bracelet or bangle", "platform": "Amazon", "estimated_price": round(budget*.12), "reason": "Optional coordinating accessory.", "url": make_link("Amazon", f"{style} bracelet bangle")},
        ]
    }

def build_prompt(planner, data):
    common = """You are PocketSmart AI, a budget-aware recommendation assistant.
Return ONLY valid JSON. Do not use markdown fences.
Do not claim that you have live inventory, live prices, or live availability.
Estimated prices must be clearly labeled estimates.
Keep the total estimated recommendation allocation <= the user's budget.
Use Indian rupees and Indian context.
Return this schema:
{
 "title": "...",
 "summary": "...",
 "budget": number,
 "allocation": [{"category":"...", "amount": number}],
 "recommendations": [
   {"name":"...", "platform":"Amazon|Flipkart|IKEA|Swiggy|Zomato|OYO",
    "estimated_price": number, "reason":"...", "url_query":"..."}
 ]
}
"""
    return common + f"\nPlanner: {planner}\nUser data:\n{json.dumps(data, ensure_ascii=False)}"

def normalize_result(result, planner, data):
    if not isinstance(result, dict):
        raise ValueError("AI returned invalid object")
    result.setdefault("title", f"{planner.title()} Recommendations")
    result.setdefault("summary", "AI-generated budget plan.")
    result["budget"] = money(result.get("budget", data.get("budget", 0)))
    result.setdefault("allocation", [])
    result.setdefault("recommendations", [])
    clean = []
    total = 0
    for r in result["recommendations"][:12]:
        if not isinstance(r, dict):
            continue
        price = max(0, money(r.get("estimated_price", 0)))
        platform = r.get("platform", "Amazon")
        if platform not in PLATFORM_URLS:
            platform = "Amazon"
        name = str(r.get("name", "Recommended item"))[:120]
        reason = str(r.get("reason", "Budget-aware suggestion."))[:300]
        query = str(r.get("url_query", name))
        clean.append({
            "name": name, "platform": platform,
            "estimated_price": price, "reason": reason,
            "url": make_link(platform, query)
        })
        total += price
    result["recommendations"] = clean
    return result

def gemini_generate(planner, data, image_bytes=None):
    if not GEMINI_API_KEY or not genai:
        return None
    prompt = build_prompt(planner, data)
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        if image_bytes:
            image = Image.open(__import__("io").BytesIO(image_bytes))
            response = model.generate_content([prompt, image])
        else:
            response = model.generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return normalize_result(json.loads(raw), planner, data)
    except Exception:
        return None

def save_recommendation(user_id, planner, request_data, result):
    conn = db()
    conn.execute(
        "INSERT INTO recommendations(user_id,planner,request_json,result_json,created_at) VALUES(?,?,?,?,?)",
        (user_id, planner, json.dumps(request_data), json.dumps(result), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "user": current_user(request)})

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
def register(name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    if len(password) < 6:
        return templates.TemplateResponse("register.html", {"request": {}, "error": "Password must be at least 6 characters."})
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)",
            (name.strip(), email.strip().lower(), hash_password(password), datetime.utcnow().isoformat())
        )
        conn.commit()
        user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return RedirectResponse("/register?error=Email+already+registered", status_code=303)
    conn.close()
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("access_token", create_token(user_id), httponly=True, samesite="lax")
    return response

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    conn.close()
    if not user or not verify_password(password, user["password_hash"]):
        return RedirectResponse("/login?error=Invalid+email+or+password", status_code=303)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("access_token", create_token(user["id"]), httponly=True, samesite="lax")
    return response

@app.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = require_user(request)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})

@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    user = require_user(request)
    conn = db()
    rows = conn.execute(
        "SELECT id,planner,request_json,result_json,created_at FROM recommendations WHERE user_id=? ORDER BY id DESC",
        (user["id"],)
    ).fetchall()
    conn.close()
    data = []
    for r in rows:
        data.append({
            "id": r["id"], "planner": r["planner"],
            "request": json.loads(r["request_json"]),
            "result": json.loads(r["result_json"]),
            "created_at": r["created_at"]
        })
    return templates.TemplateResponse("history.html", {"request": request, "user": user, "history": data})

@app.get("/planner/{planner}", response_class=HTMLResponse)
def planner_page(planner: str, request: Request):
    if planner not in ("home", "party", "jewelry"):
        raise HTTPException(404, "Planner not found")
    return templates.TemplateResponse(f"{planner}_planner.html", {"request": request, "user": current_user(request)})

@app.post("/generate-home")
async def generate_home(request: Request, budget: float = Form(...), rooms: str = Form(...),
                        style: str = Form("Modern"), lights: int = Form(1),
                        fans: int = Form(1), tables: int = Form(1), notes: str = Form("")):
    user = require_user(request)
    if budget <= 0 or budget > 10_000_000:
        raise HTTPException(400, "Enter a valid budget.")
    data = {"budget": budget, "rooms": rooms, "style": style, "lights": lights, "fans": fans, "tables": tables, "notes": notes}
    result = gemini_generate("home", data) or fallback_home(data)
    save_recommendation(user["id"], "home", data, result)
    return JSONResponse(result)

@app.post("/generate-party")
async def generate_party(request: Request, budget: float = Form(...), guests: int = Form(...),
                         event_type: str = Form(...), venue: str = Form(""),
                         food: str = Form(""), notes: str = Form("")):
    user = require_user(request)
    if budget <= 0 or guests <= 0:
        raise HTTPException(400, "Budget and guest count must be positive.")
    data = {"budget": budget, "guests": guests, "event_type": event_type, "venue": venue, "food": food, "notes": notes}
    result = gemini_generate("party", data) or fallback_party(data)
    save_recommendation(user["id"], "party", data, result)
    return JSONResponse(result)

@app.post("/generate-jewelry")
async def generate_jewelry(request: Request, budget: float = Form(...), occasion: str = Form(...),
                           style: str = Form("Elegant"), outfit_color: str = Form(""),
                           material: str = Form(""), notes: str = Form(""),
                           outfit_image: Optional[UploadFile] = File(None)):
    user = require_user(request)
    if budget <= 0:
        raise HTTPException(400, "Enter a valid budget.")
    image_bytes = None
    if outfit_image and outfit_image.filename:
        if outfit_image.content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise HTTPException(400, "Upload a JPG, PNG, or WEBP image.")
        image_bytes = await outfit_image.read()
        if len(image_bytes) > 5 * 1024 * 1024:
            raise HTTPException(400, "Image must be 5 MB or smaller.")
    data = {"budget": budget, "occasion": occasion, "style": style, "outfit_color": outfit_color, "material": material, "notes": notes, "image_uploaded": bool(image_bytes)}
    result = gemini_generate("jewelry", data, image_bytes) or fallback_jewelry(data)
    save_recommendation(user["id"], "jewelry", data, result)
    return JSONResponse(result)

@app.post("/token")
def token(email: str = Form(...), password: str = Form(...)):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
    conn.close()
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    return {"access_token": create_token(user["id"]), "token_type": "bearer"}

@app.get("/session-info")
def session_info(request: Request):
    user = current_user(request)
    return {"logged_in": bool(user), "user": user}

@app.get("/session-data")
def session_data(request: Request):
    user = require_user(request)
    conn = db()
    count = conn.execute("SELECT COUNT(*) FROM recommendations WHERE user_id=?", (user["id"],)).fetchone()[0]
    latest = conn.execute(
        "SELECT planner,created_at FROM recommendations WHERE user_id=? ORDER BY id DESC LIMIT 5",
        (user["id"],)
    ).fetchall()
    conn.close()
    return {"user": user, "recommendation_count": count, "recent": [dict(x) for x in latest]}

@app.get("/recommendations-details/{recommendation_id}")
def recommendation_details(recommendation_id: int, request: Request):
    user = require_user(request)
    conn = db()
    row = conn.execute(
        "SELECT * FROM recommendations WHERE id=? AND user_id=?",
        (recommendation_id, user["id"])
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Recommendation not found")
    return {
        "id": row["id"], "planner": row["planner"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]),
        "created_at": row["created_at"]
    }

@app.get("/startup")
def startup_status():
    return {"status": "ready", "gemini_configured": bool(GEMINI_API_KEY), "model": GEMINI_MODEL}

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
