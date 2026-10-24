import os, re, json,  asyncio, base64, threading, random, uvicorn
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # Tambahkan ini
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_DIR = os.path.join(BASE_DIR, "cookies")
os.makedirs(COOKIE_DIR, exist_ok=True)
# ——————— File Konfigurasi ————————#
USERS_DB = "users.json"
KEYS_DB = "keys.json"

# ——————— Tools ————————— #

active_sessions = {}
active_tasks = {}    # Untuk menyimpan objek Task asyncio (agar bisa di-cancel)

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

def load_db(file):
    if not os.path.exists(file):
        with open(file, 'w') as f: json.dump({}, f)
    with open(file, 'r') as f: return json.load(f)

def save_db(file, data):
    with open(file, 'w') as f: json.dump(data, f, indent=4)
def update_session(username, log=None, ss=None, running=None):
    if username not in active_sessions:
        active_sessions[username] = {"log": "Ready", "ss": "", "running": True}
    if log is not None: active_sessions[username]["log"] = f"[{datetime.now().strftime('%H:%M:%S')}] {log}"
    if ss is not None: active_sessions[username]["ss"] = ss
    if running is not None: active_sessions[username]["running"] = running
async def stream_browser_view(page, username):
    """Mengambil screenshot secara berkala untuk dikirim ke dashboard."""
    while username in active_sessions and active_sessions.get(username, {}).get("running"):
        try:
            # Screenshot dengan kualitas rendah untuk kecepatan (mencegah delay)
            shot = await page.screenshot(type='jpeg', quality=30)
            active_sessions[username]["ss"] = base64.b64encode(shot).decode()
            await asyncio.sleep(0.5) # Refresh rate 0.5 detik
        except Exception:
            break
async def handle_popups(page, username):
    """Mendeteksi dan menutup pop-up umum Instagram."""
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
            # Cek apakah pop-up muncul dengan timeout singkat (2 detik)
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
    # Setiap akun punya 'context' dan 'page' sendiri
    session_path = os.path.join(COOKIE_DIR, f"{username}.json")
    update_session(username, log=f"Starting Session: {username}", running=True)

    async with async_playwright() as p:
        # Launch browser terpisah untuk tiap akun agar tidak bentrok
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            storage_state=session_path if os.path.exists(session_path) else None,
            viewport={'width': 1280, 'height': 720}
        )
        page = await context.new_page()
        
        # Streamer khusus untuk akun ini
        asyncio.create_task(stream_browser_view(page, username))

        try:
            if action_type == "follow_follower_target":
                username_target = target.replace('@','')
                update_session(username, log=f"Membuka profil @{username_target}...")
                
                # 1. Buka Profil Target
                await page.goto(f"https://www.instagram.com/{username_target}/", wait_until="commit")
                await asyncio.sleep(3)
                await handle_popups(page, username)
                # 2. Klik Link Followers & Tunggu Navigasi
                try:
                    update_session(username, log="Membuka daftar followers...")
                    await page.locator('a[href$="/followers/"]').first.click()
                except:
                    # Fallback jika tombol tidak bisa diklik
                    await page.goto(f"https://www.instagram.com/{username_target}/followers/", wait_until="domcontentloaded")
                    await handle_popups(page, username)

                # 3. WAJIB: TUNGGU MODAL/DAFTAR TERBUKA SEMPURNA
                update_session(username, log="Menunggu modal daftar followers...")
                try:
                    # Gunakan selector yang lebih luas: cari ARIA DIALOG
                    modal_xpath = "//div[@role='dialog']"
                    # Tunggu dialognya ada dulu (attached)
                    await page.wait_for_selector(modal_xpath, state="attached", timeout=15000)
                    
                    # Sekarang cari area scroll di dalamnya secara spesifik
                    # Kita coba beberapa selector sekaligus agar tidak gagal
                    modal = page.locator('div[role="dialog"] div._aano, div[role="dialog"] ul, div[role="dialog"] div[style*="overflow"]').first
                    
                    update_session(username, log="Daftar followers terdeteksi!")
                    await asyncio.sleep(2) 
                except:
                    update_session(username, log="Gagal: Modal tidak terdeteksi.")
                    return

                # 4. MULAI LOOP SCROLL & FOLLOW
                executed = 0
                fail_count = 0
                while executed < amount and active_sessions[username]["running"]:
                    # --- LOGIKA SKIP: Hanya ambil tombol yang teksnya "Follow" atau "Ikuti" ---
                    # Tombol "Following" atau "Requested" akan otomatis terabaikan
                    btns = page.locator('div[role="dialog"] button').filter(
                        has_text=re.compile(r"^(Follow|Ikuti)$")
                    )
                    
                    count = await btns.count()

                    if count > 0:
                        btn = btns.first # Selalu ambil yang teratas dari hasil filter
                        try:
                            # Pindahkan mouse ke tengah modal agar scroll mouse wheel pas
                            box = await modal.bounding_box()
                            if box:
                                await page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                            
                            await btn.scroll_into_view_if_needed()
                            await asyncio.sleep(1)
                            
                            # Klik tombol Follow
                            await btn.click(force=True)
                            executed += 1
                            update_session(username, log=f"Berhasil Follow {executed}/{amount}")
                            
                            # Scroll tipis agar daftar bergeser ke bawah
                            await page.mouse.wheel(0, random.randint(70, 95))
                            
                            # Delay aman (25-45 detik) agar akun tidak di-flag spam
                            await asyncio.sleep(random.randint(25, 45))
                            fail_count = 0
                        except:
                            # Jika gagal klik (mungkin elemen tertutup), scroll dikit
                            await page.mouse.wheel(0, 100)
                            fail_count += 1
                    else:
                        # --- JIKA TIDAK ADA TOMBOL FOLLOW (Semua sudah di-follow di layar) ---
                        update_session(username, log="Semua di layar sudah di-follow, skip & scroll...")
                        
                        # Scroll lebih jauh untuk mencari orang yang belum di-follow
                        await page.mouse.wheel(0, 500)
                        await asyncio.sleep(3) # Tunggu loading konten baru
                        fail_count += 1

                    if fail_count > 15:
                        update_session(username, log="Target habis atau tidak ada yang bisa di-follow.")
                        break

            elif action_type == "like_hashtag":
                tag = target.replace("#", "")
                update_session(username, log=f"Membuka #{tag}...")
                
                # 1. Gunakan 'commit' agar navigasi cepat & stream tidak blank
                await page.goto(f"https://www.instagram.com/explore/tags/{tag}/", wait_until="commit")
                await handle_popups(page, username)
                # 2. Tunggu manual elemen grid foto
                update_session(username, log="Menunggu konten dimuat...")
                try:
                    # Tunggu selector postingan (grid) muncul maksimal 10 detik
                    await page.wait_for_selector('div._aabd', timeout=10000)
                    # Kasih jeda napas 2 detik agar overlay transparan IG stabil
                    await asyncio.sleep(2) 
                except:
                    update_session(username, log="Konten lambat, mencoba scroll...")
                    await page.mouse.wheel(0, 500) # Scroll dikit buat trigger load
                    await asyncio.sleep(2)

                # Jalankan multiselector Anda yang sudah berhasil sebelumnya
                if await click_post_multiselector(page):
                    update_session(username, log="Postingan terbuka!")
                    popup = page.locator('div[role="dialog"]').first
                    await popup.wait_for(state="visible")
                    await asyncio.sleep(3)
                    for i in range(amount):
                        update_session(username, log=f"Proses Post ke-{i+1}...")
                        
                        # LOGIKA LIKE
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
                        #  LOGIKA KOMEN
                        if comment:
                            try:
                                # Gunakan fill daripada type agar lebih cepat dan stabil
                                await popup.locator('textarea').first.fill(comment)
                                await asyncio.sleep(1)
                                await page.keyboard.press("Enter")
                                update_session(username, log=f"Post {i+1}: Komen Terkirim")
                            except: pass
                        
                        # LANJUT KE POST BERIKUTNYA
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
            # Bagian ini akan jalan saat task.cancel() dipanggil
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
    async with async_playwright() as p:
        # Gunakan argumen pendukung Linux/NetHunter agar tidak crash
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        
        cookie_path = f"cookies/{username}.json"
        
        # Inisialisasi status di dashboard
        active_sessions[username] = {"running": True, "log": "Checking...", "ss": ""}
        
        try:
            if not os.path.exists(cookie_path):
                active_sessions[username].update({"log": "Status: No Cookies", "running": False})
                return

            context = await browser.new_context(storage_state=cookie_path)
            page = await context.new_page()
            
            # Jalankan stream sebentar agar user bisa lihat proses check
            stream_task = asyncio.create_task(stream_browser_view(page, username))
            try:
                await page.goto("https://www.instagram.com/", wait_until="commit", timeout=20000)
            except Exception:
                pass # Abaikan jika timeout saat loading asset, kita cek elemen saja
            await asyncio.sleep(3)
            home_icon = page.locator('svg[aria-label="Home"], svg[aria-label="Beranda"], a[href="/"]').first
            explore_icon = page.locator('svg[aria-label="Explore"], svg[aria-label="Jelajahi"]').first
            
            is_active = await home_icon.is_visible(timeout=5000) or await explore_icon.is_visible(timeout=5000)

            if is_active:
                active_sessions[username].update({
                    "log": "Status: ACTIVE ✅", 
                    "running": False
                })
            else:
                # Cek apakah tertahan di halaman login
                if "login" in page.url:
                    active_sessions[username].update({"log": "Status: DEAD/LOGOUT ❌", "running": False})
                else:
                    active_sessions[username].update({"log": "Status: UNKNOWN/STUCK ⚠️", "running": False})
                    stream_task.cancel()
        except Exception as e:
            active_sessions[username].update({
                "log": f"Error: {str(e)[:20]}", 
                "running": False
            })
        finally:
            await browser.close()
# ----------- Fungi Api Engine ---------#
@app.get("/api/get_accounts")
async def get_accounts_compat():
    return active_sessions
@app.get("/api/accounts")
async def get_accounts():
    return [
        {
            "username": acc,
            "status": data.get("status", "Idle"),
            "log": data.get("log", "-"),
            "task": data.get("task", "Idle")
        }
        for acc, data in active_sessions.items()
    ]
@app.get("/api/status")
async def get_status(username: str = None):
    if not username:
        return active_sessions  # fallback biar ga error
    return active_sessions.get(username, {})

@app.post("/api/add_account")
async def add_account(data: dict):
    username = data.get("username")
    password = data.get("password")

    if not username:
        return {"status": "error"}

    # 🔥 MASUKKAN KE SESSION DULU (INI YANG KURANG)
    active_sessions[username] = {
        "status": "Starting",
        "log": "Initializing...",
        "ss": "",
        "running": True,
        "task": "Idle"
    }

    # 🔥 JALANKAN BOT
    asyncio.create_task(login_process(username, password))

    return {"status": "running"}

async def login_process(username, password):
    async with async_playwright() as p:
        # Konfigurasi browser untuk performa stabil di Kali Linux/NetHunter
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        context = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await context.new_page()

        # Mulai Streamer segera agar di dashboard tidak "No Stream"
        active_sessions[username] = {"running": True, "log": "Browser started...", "ss": ""}
        stream_task = asyncio.create_task(stream_browser_view(page, username))

        try:
            update_session(username, log="Membuka Instagram login...")
            await page.goto("https://www.instagram.com/accounts/login/", wait_until="networkidle")
            await handle_popups(page, username)
            # 1. Perkuat Selector Username & Password
            user_selector = "input[name='username'], input[aria-label*='Phone'], input[type='text']"
            pass_selector = "input[name='password'], input[aria-label*='Password'], input[type='password']"

            # Menunggu elemen muncul di layar
            await page.wait_for_selector(user_selector, timeout=20000)
            await handle_popups(page, username)
            # 2. Gunakan Pengetikan Manusia (Mencegah kegagalan Fill)
            update_session(username, log="Mengisi kredensial...")
            await page.type(user_selector, username, delay=150)
            await page.type(pass_selector, password, delay=150)

            # 3. Perkuat Klik Button Login
            login_btn = "button[type='submit'], div[role='button']:has-text('Log in')"
            await page.wait_for_selector(login_btn, state="visible")
            
            # Memastikan tombol bisa diklik (scroll jika tertutup modal)
            await page.click(login_btn)

            update_session(username, log="Verifikasi login...")
            
            # Menunggu navigasi atau deteksi checkpoint
            await page.wait_for_timeout(10000)

            if "login" not in page.url:
                update_session(username, log="LOGIN SUCCESS")
                # Simpan cookies agar tidak login ulang terus-menerus
                await context.storage_state(path=f"cookies/{username}.json")
            else:
                update_session(username, log="LOGIN FAILED (Check Creds/2FA)")

        except Exception as e:
            update_session(username, log=f"Error: {str(e)[:50]}")
        finally:
            # Tetap biarkan running False agar kotak stream di dashboard tertutup otomatis
            active_sessions[username]["running"] = False 
            stream_task.cancel()
            await browser.close()
# --- ROUTE HALAMAN ---
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
# ——————————— Api Index ———————————————#
# --- API LOGIN ---
@app.post("/api/login")
async def api_login(data: LoginSchema):
    users = load_db(USERS_DB)
    uname = data.username
    pwd = data.password

    # Validasi User
    if uname in users and users[uname]["password"] == pwd:
        # Tentukan masa aktif cookie (30 hari jika 'remember' dicentang)
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
    
    return JSONResponse(
        status_code=401, 
        content={"status": "error", "message": "Username atau Password Salah!"}
    )

# --- API REGISTER ---
@app.post("/api/register")
async def register(data: RegisterSchema):
    uname = data.username
    pwd = data.password
    email = data.email
    key_input = data.key

    users = load_db(USERS_DB)
    keys = load_db(KEYS_DB)

    # 1. Validasi Input Kosong
    if not all([uname, pwd, email, key_input]):
        return {"status": "error", "message": "Semua kolom wajib diisi!"}

    # 2. Validasi Username Ganda
    if uname in users:
        return {"status": "error", "message": "Username sudah terdaftar!"}

    # 3. Validasi License Key
    if key_input not in keys:
        return {"status": "error", "message": "License Key tidak ditemukan!"}
    
    if keys[key_input]["status"] != "unused":
        return {"status": "error", "message": "License Key sudah digunakan!"}

    # 4. Proses Simpan ke Database
    duration = keys[key_input].get("duration", 30)
    expiry_date = datetime.now() + timedelta(days=duration)
    
    users[uname] = {
        "password": pwd,
        "email": email,
        "license": key_input,
        "expiry": str(expiry_date.date()),
        "last_seen": datetime.now().isoformat(),
        "last_error": "None"
    }
    
    # Update Status Key menjadi 'used'
    keys[key_input]["status"] = "used"
    keys[key_input]["used_by"] = uname

    save_db(USERS_DB, users)
    save_db(KEYS_DB, keys)
    
    return {"status": "success", "message": "Registrasi Berhasil! Silakan Login."}

@app.get("/api/accounts")
async def accounts():
    if not os.path.exists(COOKIE_DIR):
        return []
    # Mengambil list username dari file .json di folder cookies
    return [f.replace(".json","") for f in os.listdir(COOKIE_DIR) if f.endswith(".json")]
# --- 2. API MEMBER INFO & SECURITY ---
# Sinkron dengan loadProfile() di index.html

@app.get("/api/admin/members")
async def get_all_members():
    users = load_db("users.json")
    member_list = []
    now = datetime.now()
    
    for uname, data in users.items():
        # Hitung Online Status (Aktif dalam 60 detik terakhir)
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
@app.post("/api/admin_maintenance")
async def admin_maintenance(data: dict):
    action = data.get("action")
    
    if action == "fix_chromium":
        # Paksa berhenti semua proses browser yang memakan RAM
        import subprocess
        subprocess.run(["pkill", "-f", "chromium"], check=False)
        active_sessions.clear() 
        return {"status": "success", "message": "Sistem Chromium Berhasil Direstart."}
        
    elif action == "clear_logs":
        # Reset log tampilan pada dashboard semua user
        for user in active_sessions:
            active_sessions[user]["log"] = "Logs wiped by Admin."
        return {"status": "success", "message": "Log Dashboard Berhasil Dibersihkan."}
        
    return {"status": "error", "message": "Aksi Tidak Dikenal."}
# --- MIDDLEWARE: KEAMANAN & HEARTBEAT ---
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    uname = request.cookies.get("session_user") # Mengambil username dari cookie

    # --- BAGIAN PERBAIKAN: LOGIN ADMIN ---
    # Proteksi tambahan: Hanya username tertentu yang bisa buka path /admin
    if path.startswith("/admin") or path.startswith("/api/admin"):
        if uname != "admin": # Ganti 'admin' dengan username admin Anda
            return RedirectResponse(url="/")

    # Kecualikan halaman login, register, dan file statis dari pengecekan umum
    exempt_paths = ["/login", "/api/login", "/api/register", "/api/admin_contact"]
    
    if path in exempt_paths or path.startswith("/static"):
        return await call_next(request)
    
    # Cek apakah user sudah login untuk akses halaman lainnya
    if not uname:
        if path.startswith("/api/"):
            return JSONResponse({"status": "error", "message": "Unauthorized"}, 401)
        return RedirectResponse(url="/login")

    # Update Heartbeat (Status Online) di database
    users = load_db(USERS_DB)
    if uname in users:
        users[uname]["last_seen"] = datetime.now().isoformat()
        save_db(USERS_DB, users)
    
    return await call_next(request)


app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

