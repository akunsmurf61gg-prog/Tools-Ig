import os, re, json, asyncio, base64, threading, random, uvicorn
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from supabase import create_client, Client

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ——————— Inisialisasi Supabase ————————#
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# ——————— Helper Database Supabase ————————#
def get_user_from_db(username: str):
    if not supabase: return None
    res = supabase.table("bot_users").select("*").eq("username", username).execute()
    return res.data[0] if res.data else None

def get_all_users_from_db():
    if not supabase: return {}
    res = supabase.table("bot_users").select("*").execute()
    users_dict = {}
    for row in res.data:
        uname = row["username"]
        users_dict[uname] = {
            "password": row.get("password"),
            "email": row.get("email"),
            "license": row.get("license"),
            "expiry": row.get("expiry"),
            "last_seen": row.get("last_seen"),
            "last_error": row.get("last_error", "None")
        }
    return users_dict

def save_user_to_db(username: str, user_data: dict):
    if not supabase: return
    supabase.table("bot_users").upsert({
        "username": username,
        "password": user_data.get("password"),
        "email": user_data.get("email"),
        "license": user_data.get("license"),
        "expiry": user_data.get("expiry"),
        "last_seen": user_data.get("last_seen"),
        "last_error": user_data.get("last_error", "None")
    }).execute()

def get_keys_from_db():
    if not supabase: return {}
    res = supabase.table("bot_keys").select("*").execute()
    keys_dict = {}
    for row in res.data:
        code = row["key_code"]
        keys_dict[code] = {
            "status": row.get("status"),
            "duration": row.get("duration", 30),
            "used_by": row.get("used_by")
        }
    return keys_dict

def update_key_in_db(key_code: str, status: str, used_by: str):
    if not supabase: return
    supabase.table("bot_keys").upsert({
        "key_code": key_code,
        "status": status,
        "used_by": used_by
    }).execute()

async def load_session_state(username: str):
    if not supabase: return None
    try:
        res = supabase.table("bot_sessions").select("cookies_data").eq("username", username).execute()
        if res.data and res.data[0].get("cookies_data"):
            data = res.data[0]["cookies_data"]
            return json.loads(data) if isinstance(data, str) else data
    except Exception:
        pass
    return None

async def save_session_state(context, username: str):
    if not supabase: return
    try:
        state = await context.storage_state()
        supabase.table("bot_sessions").upsert({
            "username": username,
            "cookies_data": json.dumps(state),
            "updated_at": datetime.now().isoformat()
        }).execute()
    except Exception as e:
        print(f"Error saving session state: {e}")

# ——————— Tools ————————— #

active_sessions = {}
active_tasks = {}

class TaskSchema(BaseModel):
    account: str
    target: str
    action_type: str
    amount: int
    comment_text: str = ""
    password: str = ""

class LoginSchema(BaseModel):
    username: str
    password: str
    remember: bool = False

class RegisterSchema(BaseModel):
    username: str
    password: str
    email: str
    key: str

def update_session(username, log=None, ss=None, running=None):
    if username not in active_sessions:
        active_sessions[username] = {"log": "Ready", "ss": "", "running": True}
    if log is not None: active_sessions[username]["log"] = f"[{datetime.now().strftime('%H:%M:%S')}] {log}"
    if ss is not None: active_sessions[username]["ss"] = ss
    if running is not None: active_sessions[username]["running"] = running

async def stream_browser_view(page, username):
    while username in active_sessions and active_sessions.get(username, {}).get("running"):
        try:
            shot = await page.screenshot(type='jpeg', quality=30)
            active_sessions[username]["ss"] = base64.b64encode(shot).decode()
            await asyncio.sleep(0.5)
        except Exception:
            break

async def handle_popups(page, username):
    popups = [
        "button:has-text('Not Now')",
        "button:has-text('Lain Kali')",
        "button:has-text('Simpan Info')",
        "button:has-text('Save Info')",
        "button:has-text('Dismiss')",
        "svg[aria-label='Close']"
    ]
    for selector in popups:
        try:
            if await page.locator(selector).is_visible():
                await page.locator(selector).click()
                update_session(username, log=f"Pop-up tertutup: {selector}")
                await asyncio.sleep(1)
        except:
            continue

async def click_post_multiselector(page):
    selectors = ['div._aagw', 'article a[href*="/p/"]', 'div._aabd', 'img[style*="object-fit: cover"]']
    for s in selectors:
        try:
            loc = page.locator(s).last
            if await loc.is_visible(timeout=2500):
                await loc.click(force=True)
                return True
        except: continue
    return False

async def run_bot_logic(username, target, action_type, amount, comment, password=""):
    update_session(username, log=f"Starting Session: {username}", running=True)
    storage_state = await load_session_state(username)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            storage_state=storage_state,
            viewport={'width': 1280, 'height': 720}
        )
        page = await context.new_page()
        
        asyncio.create_task(stream_browser_view(page, username))

        try:
            if action_type == "follow_follower_target":
                username_target = target.replace('@','')
                update_session(username, log=f"Membuka profil @{username_target}...")
                
                await page.goto(f"https://www.instagram.com/{username_target}/", wait_until="commit")
                await asyncio.sleep(3)
                await handle_popups(page, username)
                try:
                    update_session(username, log="Membuka daftar followers...")
                    await page.locator('a[href$="/followers/"]').first.click()
                except:
                    await page.goto(f"https://www.instagram.com/{username_target}/followers/", wait_until="domcontentloaded")
                    await handle_popups(page, username)

                update_session(username, log="Menunggu modal daftar followers...")
                try:
                    modal_xpath = "//div[@role='dialog']"
                    await page.wait_for_selector(modal_xpath, state="attached", timeout=15000)
                    modal = page.locator('div[role="dialog"] div._aano, div[role="dialog"] ul, div[role="dialog"] div[style*="overflow"]').first
                    update_session(username, log="Daftar followers terdeteksi!")
                    await asyncio.sleep(2) 
                except:
                    update_session(username, log="Gagal: Modal tidak terdeteksi.")
                    return

                executed = 0
                fail_count = 0
                while executed < amount and active_sessions[username]["running"]:
                    btns = page.locator('div[role="dialog"] button').filter(
                        has_text=re.compile(r"^(Follow|Ikuti)$")
                    )
                    count = await btns.count()

                    if count > 0:
                        btn = btns.first
                        try:
                            box = await modal.bounding_box()
                            if box:
                                await page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                            
                            await btn.scroll_into_view_if_needed()
                            await asyncio.sleep(1)
                            await btn.click(force=True)
                            executed += 1
                            update_session(username, log=f"Berhasil Follow {executed}/{amount}")
                            await page.mouse.wheel(0, random.randint(70, 95))
                            await asyncio.sleep(random.randint(25, 45))
                            fail_count = 0
                        except:
                            await page.mouse.wheel(0, 100)
                            fail_count += 1
                    else:
                        update_session(username, log="Semua di layar sudah di-follow, skip & scroll...")
                        await page.mouse.wheel(0, 500)
                        await asyncio.sleep(3)
                        fail_count += 1

                    if fail_count > 15:
                        update_session(username, log="Target habis atau tidak ada yang bisa di-follow.")
                        break

            elif action_type == "like_hashtag":
                tag = target.replace("#", "")
                update_session(username, log=f"Membuka #{tag}...")
                await page.goto(f"https://www.instagram.com/explore/tags/{tag}/", wait_until="commit")
                await handle_popups(page, username)
                update_session(username, log="Menunggu konten dimuat...")
                try:
                    await page.wait_for_selector('div._aabd', timeout=10000)
                    await asyncio.sleep(2) 
                except:
                    update_session(username, log="Konten lambat, mencoba scroll...")
                    await page.mouse.wheel(0, 500)
                    await asyncio.sleep(2)

                if await click_post_multiselector(page):
                    update_session(username, log="Postingan terbuka!")
                    popup = page.locator('div[role="dialog"]').first
                    await popup.wait_for(state="visible")
                    await asyncio.sleep(3)
                    for i in range(amount):
                        update_session(username, log=f"Proses Post ke-{i+1}...")
                        try:
                            like_btn = popup.locator("button:has(svg[aria-label='Suka'], svg[aria-label='Like'])").first
                            already_liked = await popup.locator("svg[aria-label='Batal Suka'], svg[aria-label='Unlike']").is_visible(timeout=500)
                            if not already_liked:
                               await like_btn.click(force=True, timeout=3000)
                               update_session(username, log=f"Post {i+1}: Berhasil Like")
                            else:
                               update_session(username, log=f"Post {i+1}: Skip (Sudah Like)")
                        except Exception as e:
                            try:
                               backup_like = popup.locator('svg[aria-label="Suka"], svg[aria-label="Like"]').first
                               await backup_like.click(force=True, timeout=2000)
                               update_session(username, log=f"Post {i+1}: Like (via Backup)")
                            except:
                                update_session(username, log=f"Post {i+1}: Gagal Like")
                        if comment:
                            try:
                                await popup.locator('textarea').first.fill(comment)
                                await asyncio.sleep(1)
                                await page.keyboard.press("Enter")
                                update_session(username, log=f"Post {i+1}: Komen Terkirim")
                            except: pass
                        
                        if i < amount - 1:
                            try:
                                next_btn = popup.locator('svg[aria-label*="Selanjutnya"], svg[aria-label*="Next"]').locator("xpath=..").first
                                await next_btn.click(timeout=5000)
                                await asyncio.sleep(random.randint(5, 10))
                            except:
                                update_session(username, log="Gagal ke post berikutnya, berhenti.")
                                break
                else:
                    update_session(username, log="Gagal menemukan postingan.")
        except asyncio.CancelledError:
            update_session(username, log="STOPPED FORCEFULLY", running=False)
        except Exception as e:
            update_session(username, log=f"Error: {str(e)[:40]}", running=False)
        finally:
            await browser.close()
            if username in active_tasks:
                del active_tasks[username]

@app.post("/api/check_account")
async def api_check_account(data: dict):
    username = data.get("username")
    if not username: return {"error": "No username"}
    asyncio.create_task(check_account_logic(username))
    return {"status": "started"}

async def check_account_logic(username):
    storage_state = await load_session_state(username)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        active_sessions[username] = {"running": True, "log": "Checking...", "ss": ""}
        
        try:
            if not storage_state:
                active_sessions[username].update({"log": "Status: No Cookies", "running": False})
                return

            context = await browser.new_context(storage_state=storage_state)
            page = await context.new_page()
            
            stream_task = asyncio.create_task(stream_browser_view(page, username))
            try:
                await page.goto("https://www.instagram.com/", wait_until="commit", timeout=20000)
            except Exception:
                pass
            await asyncio.sleep(3)
            home_icon = page.locator('svg[aria-label="Home"], svg[aria-label="Beranda"], a[href="/"]').first
            explore_icon = page.locator('svg[aria-label="Explore"], svg[aria-label="Jelajahi"]').first
            
            is_active = await home_icon.is_visible(timeout=5000) or await explore_icon.is_visible(timeout=5000)

            if is_active:
                active_sessions[username].update({"log": "Status: ACTIVE ✅", "running": False})
            else:
                if "login" in page.url:
                    active_sessions[username].update({"log": "Status: DEAD/LOGOUT ❌", "running": False})
                else:
                    active_sessions[username].update({"log": "Status: UNKNOWN/STUCK ⚠️", "running": False})
                    stream_task.cancel()
        except Exception as e:
            active_sessions[username].update({"log": f"Error: {str(e)[:20]}", "running": False})
        finally:
            await browser.close()

@app.get("/api/get_accounts")
async def get_accounts_compat():
    return active_sessions

@app.get("/api/status")
async def get_status(username: str = None):
    if not username:
        return active_sessions
    return active_sessions.get(username, {})

@app.post("/api/add_account")
async def add_account(data: dict):
    username = data.get("username")
    password = data.get("password")

    if not username:
        return {"status": "error"}

    active_sessions[username] = {
        "status": "Starting",
        "log": "Initializing...",
        "ss": "",
        "running": True,
        "task": "Idle"
    }

    asyncio.create_task(login_process(username, password))
    return {"status": "running"}

async def login_process(username, password):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        context = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await context.new_page()

        active_sessions[username] = {"running": True, "log": "Browser started...", "ss": ""}
        stream_task = asyncio.create_task(stream_browser_view(page, username))

        try:
            update_session(username, log="Membuka Instagram login...")
            await page.goto("https://www.instagram.com/accounts/login/", wait_until="networkidle")
            await handle_popups(page, username)
            
            user_selector = "input[name='username'], input[aria-label*='Phone'], input[type='text']"
            pass_selector = "input[name='password'], input[aria-label*='Password'], input[type='password']"

            await page.wait_for_selector(user_selector, timeout=20000)
            await handle_popups(page, username)
            
            update_session(username, log="Mengisi kredensial...")
            await page.type(user_selector, username, delay=150)
            await page.type(pass_selector, password, delay=150)

            login_btn = "button[type='submit'], div[role='button']:has-text('Log in')"
            await page.wait_for_selector(login_btn, state="visible")
            await page.click(login_btn)

            update_session(username, log="Verifikasi login...")
            await page.wait_for_timeout(10000)

            if "login" not in page.url:
                update_session(username, log="LOGIN SUCCESS")
                await save_session_state(context, username)
            else:
                update_session(username, log="LOGIN FAILED (Check Creds/2FA)")

        except Exception as e:
            update_session(username, log=f"Error: {str(e)[:50]}")
        finally:
            active_sessions[username]["running"] = False 
            stream_task.cancel()
            await browser.close()

@app.get("/", response_class=HTMLResponse)
async def index_page():
    with open("index.html", "r") as f: return f.read()

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    with open("auth.html", "r") as f: return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("admin.html", "r") as f: return f.read()

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("session_user")
    return response

@app.post("/api/login")
async def api_login(data: LoginSchema):
    users = get_all_users_from_db()
    uname = data.username
    pwd = data.password

    if uname in users and users[uname]["password"] == pwd:
        max_age = 30 * 24 * 60 * 60 if data.remember else None
        response = JSONResponse({"status": "success", "message": "Login Berhasil"})
        response.set_cookie(
            key="session_user", 
            value=uname, 
            max_age=max_age, 
            path="/", 
            httponly=True, 
            samesite="lax"
        )
        return response
    
    return JSONResponse(status_code=401, content={"status": "error", "message": "Username atau Password Salah!"})

@app.post("/api/register")
async def register(data: RegisterSchema):
    uname = data.username
    pwd = data.password
    email = data.email
    key_input = data.key

    users = get_all_users_from_db()
    keys = get_keys_from_db()

    if not all([uname, pwd, email, key_input]):
        return {"status": "error", "message": "Semua kolom wajib diisi!"}

    if uname in users:
        return {"status": "error", "message": "Username sudah terdaftar!"}

    if key_input not in keys:
        return {"status": "error", "message": "License Key tidak ditemukan!"}
    
    if keys[key_input]["status"] != "unused":
        return {"status": "error", "message": "License Key sudah digunakan!"}

    duration = keys[key_input].get("duration", 30)
    expiry_date = datetime.now() + timedelta(days=duration)
    
    user_data = {
        "password": pwd,
        "email": email,
        "license": key_input,
        "expiry": str(expiry_date.date()),
        "last_seen": datetime.now().isoformat(),
        "last_error": "None"
    }
    
    save_user_to_db(uname, user_data)
    update_key_in_db(key_input, "used", uname)
    
    return {"status": "success", "message": "Registrasi Berhasil! Silakan Login."}

@app.get("/api/accounts")
async def accounts():
    if not supabase: return []
    res = supabase.table("bot_sessions").select("username").execute()
    return [row["username"] for row in res.data]

@app.get("/api/admin/members")
async def get_all_members():
    users = get_all_users_from_db()
    member_list = []
    now = datetime.now()
    
    for uname, data in users.items():
        is_online = False
        if "last_seen" in data:
            try:
                last_seen = datetime.fromisoformat(data["last_seen"])
                if (now - last_seen).total_seconds() < 60:
                    is_online = True
            except: pass
        
        member_list.append({
            "username": uname,
            "key": data.get("license", "N/A"),
            "status": "ONLINE" if is_online else "OFFLINE",
            "expiry": data.get("expiry"),
            "last_error": data.get("last_error", "Clean")
        })
    return member_list

@app.post("/api/admin_maintenance")
async def admin_maintenance(data: dict):
    action = data.get("action")
    if action == "fix_chromium":
        import subprocess
        subprocess.run(["pkill", "-f", "chromium"], check=False)
        active_sessions.clear() 
        return {"status": "success", "message": "Sistem Chromium Berhasil Direstart."}
    elif action == "clear_logs":
        for user in active_sessions:
            active_sessions[user]["log"] = "Logs wiped by Admin."
        return {"status": "success", "message": "Log Dashboard Berhasil Dibersihkan."}
    return {"status": "error", "message": "Aksi Tidak Dikenal."}
@app.on_event("startup")
async def create_default_admin():
    if supabase:
        try:
            # Cek apakah user 'admin' sudah ada di database
            res = supabase.table("bot_users").select("username").eq("username", "admin").execute()
            if not res.data:
                # Jika belum ada, buat otomatis
                supabase.table("bot_users").insert({
                    "username": "admin",
                    "password": "123", # Ganti dengan password pilihan Anda
                    "email": "admin@system.com",
                    "license": "ADMIN-KEY",
                    "expiry": "2099-12-31",
                    "last_error": "None"
                }).execute()
                print("Default admin account created successfully!")
        except Exception as e:
            print(f"Failed to create default admin: {e}")
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    uname = request.cookies.get("session_user")

    if path.startswith("/admin") or path.startswith("/api/admin"):
        if uname != "admin":
            return RedirectResponse(url="/")

    exempt_paths = ["/login", "/api/login", "/api/register", "/api/admin_contact"]
    if path in exempt_paths or path.startswith("/static"):
        return await call_next(request)
    
    if not uname:
        if path.startswith("/api/"):
            return JSONResponse({"status": "error", "message": "Unauthorized"}, 401)
        return RedirectResponse(url="/login")

    users = get_all_users_from_db()
    if uname in users:
        users[uname]["last_seen"] = datetime.now().isoformat()
        save_user_to_db(uname, users[uname])
    
    return await call_next(request)

app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
