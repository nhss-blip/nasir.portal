"""School start page server, v2 (SQLite, staff accounts, covers, pagination).
Run:  python -m pip install flask waitress      (optional: pymupdf for PDF covers)
      python -m waitress --port=8080 app:app     (first run only: set SCHOOL_ADMIN_PASSWORD)
"""
import os, re, io, json, time, sqlite3, secrets, zipfile
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from flask import (Flask, request, session, redirect, url_for, abort, g, send_file,
                   send_from_directory, render_template_string, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
try:
    import pymupdf as fitz     # PyMuPDF: optional, makes a cover picture from each PDF's first page
except ImportError:
    try: import fitz
    except ImportError: fitz = None

BASE = Path(__file__).parent
DATA, UPLOADS = BASE / "data", BASE / "uploads"
DB = DATA / "school.db"
CATEGORIES = ["General", "Exams", "Holiday", "Event", "Urgent"]
PER_PAGE, PER_GRID = 10, 12
NEW_DAYS = {"announcements": 3, "magazines": 14}   # how long the NEW badge stays on a card
ATT, MAX_ATT = UPLOADS / "_announcements", 6      # announcement attachments live here (also in backups)
DATA.mkdir(exist_ok=True); UPLOADS.mkdir(exist_ok=True); ATT.mkdir(exist_ok=True)
KEY = DATA / "secret.key"
if not KEY.exists():
    KEY.write_text(secrets.token_hex(32))

app = Flask(__name__)
if os.environ.get("BEHIND_PROXY"):      # set by the service file when nginx sits in front
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.secret_key = KEY.read_text()
app.permanent_session_lifetime = timedelta(hours=8)
app.config.update(MAX_CONTENT_LENGTH=80 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("HTTPS_ONLY")))   # set HTTPS_ONLY=1 once the site is on https

SCHEMA = """
create table if not exists users(id integer primary key, username text unique not null, pw text not null, role text not null default 'editor');
create table if not exists magazines(id integer primary key, slug text unique not null, name text not null, sort integer default 0, hidden integer default 0);
create table if not exists issues(id integer primary key, magazine_id integer not null references magazines(id), title text not null, issue_date text not null, file text not null, cover text, author text, created text);
create table if not exists posts(id integer primary key, title text not null, body text not null, category text default 'General', pinned integer default 0, expires text, created text not null, updated text, author text);
create table if not exists attachments(id integer primary key, post_id integer not null, file text not null, name text, kind text);
create table if not exists audit(id integer primary key, ts text, user text, action text, detail text);
"""

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M")

def init():
    c = sqlite3.connect(DB); c.executescript(SCHEMA)
    if "created" not in [r[1] for r in c.execute("pragma table_info(issues)")]:   # upgrade an older database
        c.execute("alter table issues add column created text")
    c.execute("update issues set created=issue_date||' 00:00' where created is null")
    if not c.execute("select 1 from users").fetchone():
        pw = os.environ.get("SCHOOL_ADMIN_PASSWORD")
        if not pw: raise SystemExit("First run: set SCHOOL_ADMIN_PASSWORD (it creates the 'admin' account).")
        c.execute("insert into users(username,pw,role) values('admin',?,'admin')", (generate_password_hash(pw),))
    if not c.execute("select 1 from magazines").fetchone():
        c.executemany("insert into magazines(slug,name,sort) values(?,?,?)",
                      [("magazine-1", "Magazine One", 1), ("magazine-2", "Magazine Two", 2), ("magazine-3", "Magazine Three", 3)])
    old = DATA / "announcements.json"          # bring over data from the first version
    if old.exists() and not c.execute("select 1 from posts").fetchone():
        try:
            for n in json.loads(old.read_text("utf-8")):
                d = datetime.strptime(n["date"], "%d %b %Y").strftime("%Y-%m-%d 00:00")
                c.execute("insert into posts(title,body,created,author) values(?,?,?,'admin')", (n["title"], n["text"], d))
        except Exception: pass
    for m in c.execute("select id,slug from magazines").fetchall():   # register PDFs already in the folders
        for p in (UPLOADS / m[1]).glob("*.pdf"):
            if not c.execute("select 1 from issues where file=?", (p.name,)).fetchone():
                c.execute("insert into issues(magazine_id,title,issue_date,file,author,created) values(?,?,?,?,'import',?)",
                          (m[0], re.sub(r"[_-]+", " ", p.stem).strip(), date.fromtimestamp(p.stat().st_mtime).isoformat(), p.name,
                           datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")))
    c.commit(); c.close()
init()

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB, timeout=10); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def me():
    if "uid" not in session: return None
    if "me" not in g: g.me = db().execute("select * from users where id=?", (session["uid"],)).fetchone()
    return g.me

def guard(admin=False):
    u = me()
    if not u: abort(redirect(url_for("login")))
    if admin and u["role"] != "admin": abort(403)
    if request.method == "POST" and request.form.get("csrf") != session.get("csrf"): abort(400)

def log(action, detail=""):
    u = me()
    db().execute("insert into audit(ts,user,action,detail) values(?,?,?,?)", (now(), u["username"] if u else "-", action, detail[:200]))
    db().commit()

def qs(n):
    a = request.args.to_dict(); a["page"] = n
    return urlencode(a)

def page(title, name, **kw):
    session.setdefault("csrf", secrets.token_hex(16))
    return render_template_string(T, title=title, page=name, tok=session["csrf"], user=me(), cats=CATEGORIES, qs=qs, **kw)

def back(msg): return redirect(url_for("admin", m=msg))
def pages(n, per): return max(1, -(-n // per))

def make_cover(path):
    if not fitz: return None
    try:
        with fitz.open(str(path)) as d:
            out = path.with_suffix(".jpg")
            d[0].get_pixmap(matrix=fitz.Matrix(0.7, 0.7)).save(str(out))
        return out.name
    except Exception: return None

def backfill_covers():     # at start-up, make covers for issues that don't have one yet
    if not fitz: return
    c = sqlite3.connect(DB)
    for i, f, s in c.execute("select i.id,i.file,m.slug from issues i join magazines m on m.id=i.magazine_id where i.cover is null").fetchall():
        cv = make_cover(UPLOADS / s / f)
        if cv: c.execute("update issues set cover=? where id=?", (cv, i))
    c.commit(); c.close()
backfill_covers()

MIME = {"pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
def sniff(h):      # decide the type from the file's first bytes, not its name
    if h[:5] == b"%PDF-": return "pdf"
    if h[:3] == b"\xff\xd8\xff": return "jpg"
    if h[:4] == b"\x89PNG": return "png"
    if h[:4] == b"GIF8": return "gif"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP": return "webp"

def save_files(pid, files):
    d = db(); ok = bad = 0
    have = d.execute("select count(*) from attachments where post_id=?", (pid,)).fetchone()[0]
    for f in files:
        if not f or not f.filename: continue
        ext = sniff(f.stream.read(12)); f.stream.seek(0)
        if not ext or have + ok >= MAX_ATT: bad += 1; continue
        name = secrets.token_hex(8) + "." + ext; f.save(ATT / name)
        d.execute("insert into attachments(post_id,file,name,kind) values(?,?,?,?)",
                  (pid, name, Path(f.filename).name[:100], "pdf" if ext == "pdf" else "img")); ok += 1
    return ok, bad

def drop_atts(where, arg):
    for a in db().execute(f"select file from attachments where {where}", (arg,)).fetchall():
        (ATT / a["file"]).unlink(missing_ok=True)
    db().execute(f"delete from attachments where {where}", (arg,))

# ---------- public ----------
@app.route("/")
def home(): return send_from_directory(BASE, "index.html")

@app.route("/images/<path:f>")
def images(f): return send_from_directory(BASE / "images", f)

def serve(i, col, mime):
    r = db().execute(f"select i.{col} f, m.slug from issues i join magazines m on m.id=i.magazine_id where i.id=?", (i,)).fetchone()
    if not r or not r["f"]: abort(404)
    resp = send_from_directory(UPLOADS / r["slug"], r["f"], mimetype=mime)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

@app.route("/files/<int:i>")
def pdf(i): return serve(i, "file", "application/pdf")

@app.route("/cover/<int:i>")
def cover(i): return serve(i, "cover", "image/jpeg")

@app.route("/api/new")
def api_new():    # which cards get a NEW badge on the main page
    cut = lambda k: (datetime.now() - timedelta(days=NEW_DAYS[k])).strftime("%Y-%m-%d %H:%M")
    d = db()
    news = d.execute("select 1 from posts where created>=? and (expires is null or expires='' or expires>=date('now','localtime')) limit 1", (cut("announcements"),)).fetchone()
    mags = d.execute("select 1 from issues i join magazines m on m.id=i.magazine_id where m.hidden=0 and i.created>=? limit 1", (cut("magazines"),)).fetchone()
    r = jsonify({"/announcements": bool(news), "/magazines": bool(mags)})
    r.headers["Cache-Control"] = "no-store"
    return r

@app.route("/att/<int:i>")
def att(i):
    r = db().execute("select file from attachments where id=?", (i,)).fetchone() or abort(404)
    resp = send_from_directory(ATT, r["file"], mimetype=MIME[r["file"].rsplit(".", 1)[1]])
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

@app.route("/magazines")
@app.route("/magazines/<slug>")
def magazines(slug=None):
    d = db()
    if slug:
        m = d.execute("select * from magazines where slug=?", (slug,)).fetchone() or abort(404)
        pg = max(1, request.args.get("page", 1, int))
        n = d.execute("select count(*) from issues where magazine_id=?", (m["id"],)).fetchone()[0]
        rows = d.execute("select * from issues where magazine_id=? order by issue_date desc,id desc limit ? offset ?",
                         (m["id"], PER_GRID, (pg - 1) * PER_GRID)).fetchall()
        return page(m["name"], "issues", m=m, items=rows, pg=pg, pages=pages(n, PER_GRID))
    blocks = [(m, d.execute("select * from issues where magazine_id=? order by issue_date desc,id desc limit 4", (m["id"],)).fetchall())
              for m in d.execute("select * from magazines where hidden=0 order by sort,id").fetchall()]
    return page("Magazines", "mag", blocks=blocks)

@app.route("/announcements")
def announcements():
    q, cat = request.args.get("q", "").strip(), request.args.get("cat", "")
    pg = max(1, request.args.get("page", 1, int))
    where, a = ["(expires is null or expires='' or expires >= date('now','localtime'))"], []
    if q: where.append("(title like ? or body like ?)"); a += [f"%{q}%"] * 2
    if cat in CATEGORIES: where.append("category=?"); a.append(cat)
    w = " and ".join(where)
    n = db().execute(f"select count(*) from posts where {w}", a).fetchone()[0]
    rows = db().execute(f"select * from posts where {w} order by pinned desc, created desc limit ? offset ?",
                        a + [PER_PAGE, (pg - 1) * PER_PAGE]).fetchall()
    atts = {}
    if rows:
        ids = [r["id"] for r in rows]
        for a in db().execute(f"select * from attachments where post_id in ({','.join('?' * len(ids))}) order by id", ids):
            atts.setdefault(a["post_id"], []).append(a)
    return page("Announcements", "news", items=rows, atts=atts, q=q, cat=cat, pg=pg, pages=pages(n, PER_PAGE))

# ---------- staff ----------
FAILS = {}
@app.route("/login", methods=["GET", "POST"])
def login():
    ip, err = request.remote_addr, ""
    f = FAILS.get(ip, [0, 0])
    if request.method == "POST":
        if f[1] > time.time():
            err = "Too many attempts. Try again in 10 minutes."
        else:
            u = db().execute("select * from users where username=?", (request.form.get("username", "").strip().lower(),)).fetchone()
            if u and check_password_hash(u["pw"], request.form.get("password", "")):
                FAILS.pop(ip, None); session.clear(); session.permanent = True; session["uid"] = u["id"]
                log("login"); return redirect(url_for("admin"))
            f[0] += 1
            FAILS[ip] = [0, time.time() + 600] if f[0] >= 5 else f
            err = "Wrong username or password."
    return page("Staff login", "login", err=err)

@app.post("/logout")
def logout():
    guard(); session.clear(); return redirect("/")

@app.route("/admin")
def admin():
    guard(); d = db()
    edit = d.execute("select * from posts where id=?", (request.args.get("edit", 0, int),)).fetchone()
    edit_atts = d.execute("select * from attachments where post_id=?", (edit["id"],)).fetchall() if edit else []
    return page("Staff area", "admin", edit=edit, edit_atts=edit_atts, msg=request.args.get("m", ""), today=date.today().isoformat(),
        posts=d.execute("select * from posts order by pinned desc, created desc limit 40").fetchall(),
        mags=d.execute("select * from magazines order by sort,id").fetchall(),
        issues=d.execute("select i.*, m.name mag from issues i join magazines m on m.id=i.magazine_id order by i.id desc limit 40").fetchall(),
        users=d.execute("select * from users order by id").fetchall(),
        audit=d.execute("select * from audit order by id desc limit 12").fetchall())

@app.post("/admin/post")
def save_post():
    guard(); f = request.form
    t, b = f.get("title", "").strip()[:150], f.get("body", "").strip()[:5000]
    if not (t and b): return back("Title and message are required.")
    cat = f.get("category") if f.get("category") in CATEGORIES else "General"
    vals = (t, b, cat, int(bool(f.get("pinned"))), f.get("expires") or None)
    pid = f.get("id", type=int)
    if pid:
        db().execute("update posts set title=?,body=?,category=?,pinned=?,expires=?,updated=? where id=?", vals + (now(), pid))
        for a in f.getlist("remove"):
            if a.isdigit(): drop_atts("id=? and post_id=" + str(pid), int(a))
    else:
        pid = db().execute("insert into posts(title,body,category,pinned,expires,created,author) values(?,?,?,?,?,?,?)",
                           vals + (now(), me()["username"])).lastrowid
    ok, bad = save_files(pid, request.files.getlist("files"))
    log("edit post" if f.get("id") else "post", t); db().commit()
    return back("Saved." + (f" {bad} file(s) skipped: only JPG, PNG, GIF, WEBP or PDF, up to {MAX_ATT} per announcement." if bad else ""))

@app.post("/admin/upload")
def upload():
    guard(); f = request.files.get("pdf")
    m = db().execute("select * from magazines where id=?", (request.form.get("mag", type=int),)).fetchone()
    if not m or not f or not f.filename: return back("Choose a magazine and a PDF.")
    head = f.stream.read(5); f.stream.seek(0)
    if not f.filename.lower().endswith(".pdf") or head != b"%PDF-": return back("Only PDF files are allowed.")
    title = request.form.get("title", "").strip()[:120] or re.sub(r"[_-]+", " ", Path(f.filename).stem).strip()
    folder = UPLOADS / m["slug"]; folder.mkdir(exist_ok=True)
    name = secrets.token_hex(8) + ".pdf"; f.save(folder / name)
    db().execute("insert into issues(magazine_id,title,issue_date,file,cover,author,created) values(?,?,?,?,?,?,?)",
                 (m["id"], title, request.form.get("date") or date.today().isoformat(), name, make_cover(folder / name), me()["username"], now()))
    log("upload", f"{m['name']}: {title}"); db().commit()
    return back("Uploaded " + title)

@app.post("/admin/del/<kind>/<int:i>")
def delete(kind, i):
    guard(kind == "user"); d = db()
    if kind == "post": drop_atts("post_id=?", i); d.execute("delete from posts where id=?", (i,))
    elif kind == "issue":
        r = d.execute("select i.file,i.cover,m.slug from issues i join magazines m on m.id=i.magazine_id where i.id=?", (i,)).fetchone()
        if r:
            for n in (r["file"], r["cover"]):
                if n: (UPLOADS / r["slug"] / n).unlink(missing_ok=True)
            d.execute("delete from issues where id=?", (i,))
    elif kind == "user" and i != me()["id"]: d.execute("delete from users where id=?", (i,))
    else: abort(404)
    d.commit(); log("delete " + kind, str(i))
    return back("Deleted.")

@app.post("/admin/mag")
def mag():
    guard(True); d = db(); name = request.form.get("name", "").strip()[:60]
    if not name: return back("Name is required.")
    mid = request.form.get("id", type=int)
    if mid: d.execute("update magazines set name=? where id=?", (name, mid))
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "magazine"
        if d.execute("select 1 from magazines where slug=?", (slug,)).fetchone(): slug += "-" + secrets.token_hex(2)
        d.execute("insert into magazines(slug,name,sort) values(?,?,(select coalesce(max(sort),0)+1 from magazines))", (slug, name))
    d.commit(); log("magazine", name)
    return back("Magazine saved.")

@app.post("/admin/user")
def add_user():
    guard(True); f = request.form
    n, pw = re.sub(r"[^a-z0-9._-]", "", f.get("username", "").lower()), f.get("password", "")
    if not n or len(pw) < 8: return back("Username and a password of 8+ characters are required.")
    try: db().execute("insert into users(username,pw,role) values(?,?,?)", (n, generate_password_hash(pw), "admin" if f.get("role") == "admin" else "editor"))
    except sqlite3.IntegrityError: return back("That username exists.")
    db().commit(); log("add user", n)
    return back("User added.")

@app.post("/admin/password")
def password():
    guard(); f = request.form
    if not check_password_hash(me()["pw"], f.get("old", "")) or len(f.get("new", "")) < 8:
        return back("Current password wrong, or new one shorter than 8 characters.")
    db().execute("update users set pw=? where id=?", (generate_password_hash(f["new"]), me()["id"])); db().commit()
    log("password changed"); return back("Password changed.")

@app.route("/admin/backup")
def backup():
    guard(True); tmp = DATA / "backup.tmp"
    s, t = sqlite3.connect(DB), sqlite3.connect(tmp); s.backup(t); t.close(); s.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "school.db")
        for p in UPLOADS.rglob("*"):
            if p.is_file(): z.write(p, "uploads/" + p.relative_to(UPLOADS).as_posix())
    tmp.unlink(); buf.seek(0); log("backup")
    return send_file(buf, as_attachment=True, download_name=f"school-backup-{date.today()}.zip")

# ---------- design ----------
T = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}}</title>
<style>
@font-face{font-family:UrduFont;src:local("Jameel Noori Nastaleeq"),local("Noto Nastaliq Urdu"),local("Urdu Typesetting");unicode-range:U+0600-06FF,U+0750-077F,U+FB50-FDFF,U+FE70-FEFF}
:root{--ink:#f1e9d6;--mute:#b7aebd;--gold:#dcb865;--line:rgba(255,255,255,.08);--ease:cubic-bezier(.2,.7,.2,1)}
*{box-sizing:border-box}
body{margin:0;background:#120d1a url(/images/background.jpg) center/cover fixed;color:var(--ink);font:17px/1.75 UrduFont,system-ui,sans-serif}
body::before{content:"";position:fixed;inset:0;background:rgba(18,13,26,.6);z-index:-1}
.wrap{display:grid;grid-template-columns:minmax(220px,310px) minmax(0,1fr);gap:clamp(2rem,6vw,6rem);max-width:1240px;margin:0 auto;padding:clamp(2rem,7vw,6rem) 1.5rem}
.side{position:sticky;top:2rem;align-self:start}
.logo{height:92px;width:auto;display:block;margin-bottom:1.2rem}
h1{font:400 clamp(2.4rem,5vw,3.8rem)/1.05 Georgia,serif;letter-spacing:-.03em;color:var(--gold);margin:.4rem 0 1.4rem}
h2{font:400 1.55rem/1.25 UrduFont,Georgia,serif;letter-spacing:-.01em;color:var(--gold);margin:.2rem 0 .6rem}
a{color:var(--gold)}.mute,time{color:var(--mute);font-size:.9rem}
.card{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:22px;padding:1.5rem 1.8rem;margin:0 0 1.4rem;box-shadow:0 1px 2px rgba(0,0,0,.25),0 14px 40px rgba(0,0,0,.28);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}
.card:nth-of-type(even){margin-left:clamp(0rem,3vw,2.5rem)}
.chips{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:1rem}
.chip{display:inline-flex;align-items:center;min-height:44px;padding:0 1rem;border:1px solid var(--line);border-radius:999px;color:var(--ink);text-decoration:none;font-size:.9rem;transition:all .2s var(--ease)}
.card .chip{min-height:30px;padding:0 .8rem;font-size:.8rem;cursor:default}
.chip:hover{border-color:var(--gold)}.chip.on{background:var(--gold);color:#1b1226;border-color:var(--gold)}.chip.urgent{border-color:#e5736b;color:#f3a29b}
.meta{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-bottom:.4rem}
.post p{white-space:pre-line;margin:.3rem 0 0}
.att{display:flex;flex-wrap:wrap;gap:.8rem;margin-top:1rem}
.att img{display:block;max-width:100%;max-height:200px;border-radius:12px;border:1px solid var(--line);transition:transform .25s var(--ease)}
.att a:hover img{transform:scale(1.02)}
.card .chip.file{cursor:pointer;max-width:100%;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;display:inline-block;line-height:30px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:1.3rem;margin-top:1rem}
.iss{display:block;color:inherit;text-decoration:none;transition:transform .25s var(--ease)}
.iss:hover{transform:translateY(-5px)}.iss:active{transform:scale(.98)}
.cv{aspect-ratio:3/4;border-radius:12px;overflow:hidden;display:grid;place-items:center;margin-bottom:.5rem;font:3rem Georgia,serif;color:var(--gold);background:linear-gradient(160deg,#3a2460,#1a1128);border:1px solid rgba(220,184,101,.3);box-shadow:0 12px 28px rgba(0,0,0,.4)}
.cv img{width:100%;height:100%;object-fit:cover}.iss b{display:block;font-weight:600;line-height:1.35}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:1rem}.head a{text-decoration:none}
input,textarea,select{width:100%;min-height:44px;padding:.6rem .9rem;margin:.25rem 0 .8rem;border-radius:12px;border:1px solid var(--line);background:rgba(255,255,255,.92);color:#202124;font:inherit;font-size:1rem}
input[type=checkbox]{width:auto;min-height:0;margin-right:.5rem}
button{min-height:44px;padding:0 1.5rem;border:0;border-radius:999px;background:var(--gold);color:#1b1226;font:600 1rem system-ui;cursor:pointer;transition:transform .15s var(--ease),filter .2s}
button:hover{filter:brightness(1.08)}button:active{transform:scale(.98)}
button.ghost{background:transparent;color:var(--ink);border:1px solid var(--line);font-weight:400;min-height:36px;padding:0 1rem}
.row{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:.5rem 0;border-top:1px solid var(--line)}
.row form{margin:0}.row input{margin:0}
.pager{display:flex;gap:1.2rem;align-items:center;justify-content:center;margin-top:1.5rem}.pager a{padding:.6rem 1rem}
:focus-visible{outline:2px solid var(--gold);outline-offset:3px}
@media(max-width:820px){.wrap{grid-template-columns:1fr;gap:1.5rem}.side{position:static}.card:nth-of-type(even){margin-left:0}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
{% macro tile(i,m) %}<a class="iss" href="/files/{{i.id}}" target="_blank" rel="noopener"><div class="cv">{% if i.cover %}<img src="/cover/{{i.id}}" alt="" loading="lazy">{% else %}{{m.name[0]}}{% endif %}</div><b dir="auto">{{i.title}}</b><span class="mute">{{i.issue_date}}</span></a>{% endmacro %}
{% macro pager() %}{% if pages>1 %}<nav class="pager">{% if pg>1 %}<a href="?{{qs(pg-1)}}">&larr; Newer</a>{% endif %}<span class="mute">{{pg}} / {{pages}}</span>{% if pg<pages %}<a href="?{{qs(pg+1)}}">Older &rarr;</a>{% endif %}</nav>{% endif %}{% endmacro %}
<div class="wrap"><aside class="side">
<a href="/"><img class="logo" src="/images/logo.webp" alt="Home" onerror="this.remove()"></a>
<a href="/">&larr; Home</a><h1>{{title}}</h1>
{% if page=='news' %}<form action="/announcements"><input type="search" name="q" value="{{q}}" placeholder="Search announcements" aria-label="Search"></form>
<div class="chips"><a class="chip{% if not cat %} on{% endif %}" href="/announcements">All</a>{% for c in cats %}<a class="chip{% if cat==c %} on{% endif %}" href="/announcements?cat={{c}}">{{c}}</a>{% endfor %}</div>{% endif %}
{% if page=='issues' %}<a href="/magazines">All magazines</a>{% endif %}
{% if page=='admin' %}<p class="mute">Signed in as {{user.username}} ({{user.role}})</p><form method="post" action="/logout"><input type="hidden" name="csrf" value="{{tok}}"><button class="ghost">Log out</button></form>{% endif %}
</aside><main>
{% if page=='mag' %}{% for m,rows in blocks %}<section class="card"><div class="head"><h2>{{m.name}}</h2><a class="mute" href="/magazines/{{m.slug}}">All issues &rarr;</a></div>
{% if rows %}<div class="grid">{% for i in rows %}{{tile(i,m)}}{% endfor %}</div>{% else %}<p class="mute">No issues yet.</p>{% endif %}</section>{% endfor %}
{% elif page=='issues' %}<section class="card">{% if items %}<div class="grid">{% for i in items %}{{tile(i,m)}}{% endfor %}</div>{% else %}<p class="mute">No issues yet.</p>{% endif %}</section>{{pager()}}
{% elif page=='news' %}{% for p in items %}<article class="card post"><div class="meta">{% if p.pinned %}<span class="chip on">Pinned</span>{% endif %}<span class="chip{% if p.category=='Urgent' %} urgent{% endif %}">{{p.category}}</span><time>{{p.created[:10]}}</time></div><h2 dir="auto">{{p.title}}</h2><p dir="auto">{{p.body}}</p>{% if atts.get(p.id) %}<div class="att">{% for a in atts[p.id] %}{% if a.kind=='img' %}<a href="/att/{{a.id}}" target="_blank" rel="noopener"><img src="/att/{{a.id}}" alt="{{a.name}}" loading="lazy"></a>{% else %}<a class="chip file" href="/att/{{a.id}}" target="_blank" rel="noopener">PDF &middot; {{a.name}}</a>{% endif %}{% endfor %}</div>{% endif %}</article>
{% else %}<p class="mute">Nothing here yet.</p>{% endfor %}{{pager()}}
{% elif page=='login' %}<form class="card" method="post"><p class="mute">{{err}}</p><input name="username" placeholder="Username" autofocus required><input type="password" name="password" placeholder="Password" required><button>Log in</button></form>
{% else %}<p class="mute">{{msg}}</p>
<form class="card" method="post" action="/admin/post" enctype="multipart/form-data"><h2>{{'Edit' if edit else 'New'}} announcement</h2><input type="hidden" name="csrf" value="{{tok}}">{% if edit %}<input type="hidden" name="id" value="{{edit.id}}">{% endif %}
<input name="title" dir="auto" placeholder="Title" maxlength="150" value="{{edit.title if edit}}" required><textarea name="body" dir="auto" rows="5" placeholder="Message (Urdu or English)" required>{{edit.body if edit}}</textarea>
<select name="category">{% for c in cats %}<option{% if edit and edit.category==c %} selected{% endif %}>{{c}}</option>{% endfor %}</select>
<label class="mute">Hide after (optional)</label><input type="date" name="expires" value="{{edit.expires if edit and edit.expires}}"><label class="mute">Attach images or PDFs (optional, up to 6)</label><input type="file" name="files" multiple accept="image/*,.pdf,application/pdf">{% for a in edit_atts %}<label class="mute"><input type="checkbox" name="remove" value="{{a.id}}">Remove {{a.name}}</label><br>{% endfor %}<label><input type="checkbox" name="pinned"{% if edit and edit.pinned %} checked{% endif %}>Pin to top</label><br><br><button>{{'Save changes' if edit else 'Post'}}</button></form>
<form class="card" method="post" action="/admin/upload" enctype="multipart/form-data"><h2>Upload a magazine issue</h2><input type="hidden" name="csrf" value="{{tok}}">
<select name="mag">{% for m in mags %}<option value="{{m.id}}">{{m.name}}</option>{% endfor %}</select><input name="title" placeholder="Issue title, e.g. March 2026"><input type="date" name="date" value="{{today}}"><input type="file" name="pdf" accept=".pdf,application/pdf" required><button>Upload</button></form>
<section class="card"><h2>Announcements</h2>{% for p in posts %}<div class="row"><span dir="auto">{{p.title}} <span class="mute">{{p.created[:10]}}{% if p.expires and p.expires<today %} · hidden{% endif %}</span></span><span style="display:flex;gap:.5rem"><a class="chip" href="/admin?edit={{p.id}}">Edit</a><form method="post" action="/admin/del/post/{{p.id}}"><input type="hidden" name="csrf" value="{{tok}}"><button class="ghost">Delete</button></form></span></div>{% else %}<p class="mute">None yet.</p>{% endfor %}</section>
<section class="card"><h2>Magazine issues</h2>{% for i in issues %}<div class="row"><span dir="auto">{{i.title}} <span class="mute">{{i.mag}} · {{i.issue_date}}</span></span><form method="post" action="/admin/del/issue/{{i.id}}"><input type="hidden" name="csrf" value="{{tok}}"><button class="ghost">Delete</button></form></div>{% else %}<p class="mute">None yet.</p>{% endfor %}</section>
<form class="card" method="post" action="/admin/password"><h2>Change my password</h2><input type="hidden" name="csrf" value="{{tok}}"><input type="password" name="old" placeholder="Current password" required><input type="password" name="new" placeholder="New password (8+ characters)" required><button>Change</button></form>
{% if user.role=='admin' %}<section class="card"><h2>Magazines</h2>{% for m in mags %}<form class="row" method="post" action="/admin/mag"><input type="hidden" name="csrf" value="{{tok}}"><input type="hidden" name="id" value="{{m.id}}"><input name="name" value="{{m.name}}"><button class="ghost">Rename</button></form>{% endfor %}
<form class="row" method="post" action="/admin/mag"><input type="hidden" name="csrf" value="{{tok}}"><input name="name" placeholder="Add a magazine"><button class="ghost">Add</button></form></section>
<section class="card"><h2>Staff accounts</h2>{% for u in users %}<div class="row"><span>{{u.username}} <span class="mute">{{u.role}}</span></span>{% if u.id!=user.id %}<form method="post" action="/admin/del/user/{{u.id}}"><input type="hidden" name="csrf" value="{{tok}}"><button class="ghost">Remove</button></form>{% endif %}</div>{% endfor %}
<form method="post" action="/admin/user" style="margin-top:1rem"><input type="hidden" name="csrf" value="{{tok}}"><input name="username" placeholder="Username" required><input type="password" name="password" placeholder="Password (8+ characters)" required><select name="role"><option value="editor">Editor (posts and uploads)</option><option value="admin">Admin (also manages staff)</option></select><button>Add staff</button></form></section>
<section class="card"><h2>Backup and activity</h2><p><a href="/admin/backup">Download a full backup (.zip)</a></p>{% for a in audit %}<div class="row"><span class="mute">{{a.ts}} · {{a.user}} · {{a.action}} {{a.detail}}</span></div>{% endfor %}</section>{% endif %}
{% endif %}</main></div></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
