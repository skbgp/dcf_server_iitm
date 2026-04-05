# main.py - FastAPI server for student registration, submissions, and admin dashboard
# Author: Shubham (CS25M046) — https://github.com/skbgp/dcf_server_iitm
# Copyright (c) 2026 Shubham (CS25M046). All rights reserved. See LICENSE.

import os
import json
import socket
import asyncio
import csv
import re
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime

# ── Project metadata (do not modify — changing these will break the server) ──
__author__  = "Shubham"
__roll__    = "CS25M046"
__project__ = "DCF Exam Server"
__repo__    = "https://github.com/skbgp/dcf_server_iitm"
__license__ = "MIT"
_AUTHOR_SIG = "e75a0bbc9d111457cf1ab8b910dc838c1baa0bcdfea2f889ce651348deab34ab"

def _verify_integrity():
    """Verify project metadata hasn't been tampered with. Returns True if valid."""
    computed = hashlib.sha256(f"{__author__}:{__roll__}:{__repo__}".encode()).hexdigest()
    return computed == _AUTHOR_SIG
# ──────────────────────────────────────────────────────────────────────
from fastapi import FastAPI, UploadFile, Form, File, Request, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
from typing import Any, Dict, Optional, List, Tuple
from celery.result import AsyncResult
from task import handle_submission

# File paths
STUDENTS_FILE = ".active_lab/students.txt"
REGISTRATIONS_FILE = ".active_lab/registrations.csv"
VIOLATIONS_FILE = ".active_lab/violations.csv"
SYSTEM_ACCESS_FILE = ".active_lab/system_access.txt"
RECOVERY_REQUESTS_FILE = ".active_lab/recovery_requests.json"

# In-memory caches
# Cache student list to minimize disk reads
_student_list_cache = set()
_student_list_mtime = 0.0

def get_student_list() -> set:
    """Return the current set of authorized roll numbers.
    
    Re-reads from disk only when the file's modification time changes,
    so it's cheap to call on every request.
    """
    global _student_list_cache, _student_list_mtime
    try:
        current_mtime = os.path.getmtime(STUDENTS_FILE)
    except OSError:
        return _student_list_cache
    if current_mtime != _student_list_mtime:
        new_set = set()
        with open(STUDENTS_FILE, mode='r', encoding='utf-8') as f:
            for line in f:
                val = line.strip().upper()
                if val:
                    new_set.add(val)
        _student_list_cache = new_set
        _student_list_mtime = current_mtime
    return _student_list_cache

# Maps each roll number to their registered IP and timestamp.
ip_roll_map: Dict[str, Dict[str, str]] = {}

# Set of IP patterns currently allowed to access the server.
allowed_systems: set[str] = set()

# The server's own LAN IP, detected at startup.
SERVER_IP = "127.0.0.1"
SERVER_SUBNET = ""
SERVER_WIFI_PREFIX = ""

# Hardcoded list of "safe" subnet prefixes.
LAB_SUBNETS = ["10.21.225", "10.21.224", "127.0.0.1"]

# Async lock to prevent concurrent writes to CSV files.
file_lock = asyncio.Lock()

# SSE listeners for real-time violation updates on the admin panel.
violation_listeners = []

# Simple rate limiter: tracks the last submission time per roll number.
_submission_cooldowns: Dict[str, float] = {}
SUBMIT_COOLDOWN_SECONDS = 5

# Approvals tracking for code recovery requests.
recovery_requests: Dict[str, Dict[str, dict]] = {}
total_recovery_count = 0  # Cumulative count for notifications

def save_recovery_requests():
    """Persist recovery requests to disk so they survive server restarts."""
    try:
        data = {"requests": recovery_requests, "total_count": total_recovery_count}
        with open(RECOVERY_REQUESTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[RECOVERY] Warning: failed to save recovery requests: {e}")

def load_recovery_requests():
    """Load recovery requests from disk on startup."""
    global recovery_requests, total_recovery_count
    if os.path.exists(RECOVERY_REQUESTS_FILE):
        try:
            with open(RECOVERY_REQUESTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            recovery_requests.update(data.get("requests", {}))
            total_recovery_count = data.get("total_count", 0)
            pending = sum(1 for r in recovery_requests.values() for info in r.values() if info.get("status") == "pending")
            print(f"Loaded {pending} pending recovery requests from disk.")
        except Exception as e:
            print(f"[RECOVERY] Warning: failed to load recovery requests: {e}")

# App lifecycle: restore state from disk on startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify project integrity — server will not start if metadata is tampered
    if not _verify_integrity():
        print("\n" + "="*60)
        print("FATAL: Project integrity check failed.")
        print("The project metadata has been modified.")
        print(f"Original author: Shubham (CS25M046)")
        print(f"Repository: https://github.com/skbgp/dcf_server_iitm")
        print("Restore the original metadata to start the server.")
        print("="*60 + "\n")
        import sys; sys.exit(1)
    
    print(f"{__project__} by {__author__} ({__roll__})")
    initial_students = get_student_list()
    
    if len(initial_students) == 0:
        print("No student in the class. Exiting...")
        return
    
    # Restore registration map
    if os.path.exists(REGISTRATIONS_FILE):
        with open(REGISTRATIONS_FILE, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            try:
                next(reader)  # skip the header row (roll_no, ip_address, timestamp)
            except StopIteration:
                pass  # file exists but is empty — that's fine
            
            for row in reader:
                if row and len(row) >= 2:
                    roll_no = row[0]
                    ip_address = row[1]
                    timestamp = row[2] if len(row) >= 3 else "Unknown"
                    ip_roll_map[roll_no] = {"ip": ip_address, "timestamp": timestamp}
    
    # Restore whitelist
    if os.path.exists(SYSTEM_ACCESS_FILE):
        with open(SYSTEM_ACCESS_FILE, mode='r', encoding='utf-8') as f:
            for line in f:
                sys = normalize_ip(line)
                if sys:
                    allowed_systems.add(sys)

    # Restore recovery requests
    load_recovery_requests()

    print(f"Loaded {len(ip_roll_map)} registrations into memory.")
    print(f"Loaded {len(allowed_systems)} allowed systems.")

    # Figure out our own IP so we can identify admin requests (admin = server machine).
    global SERVER_IP, SERVER_SUBNET, SERVER_WIFI_PREFIX
    local_ip = get_local_ip()
    SERVER_IP = local_ip
    octets = local_ip.split('.')
    if len(octets) == 4:
        SERVER_SUBNET = '.'.join(octets[:3])
        SERVER_WIFI_PREFIX = '.'.join(octets[:2])
        print(f"🔒 Network security enabled:")
        print(f"   LAN subnet (students): {SERVER_SUBNET}.*")
        print(f"   WiFi prefix (admins):   {SERVER_WIFI_PREFIX}.*.*")
    else:
        print("⚠️  Could not detect network subnet. Network restrictions disabled.")

    yield  # --- server runs here ---
    print("Application shutting down...")

# Create the FastAPI app. We disable the default /docs and /redoc routes
# because we have our own docs hub at /docs that serves offline C++ references.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

@app.get("/api/about")
async def about():
    """Public endpoint — project metadata and authorship info."""
    valid = _verify_integrity()
    return JSONResponse({
        "project": __project__,
        "author": __author__,
        "roll": __roll__,
        "repo": __repo__,
        "license": __license__,
        "integrity": "verified" if valid else "TAMPERED"
    })

# Author credit watermark (injected server-side into every HTML page)

_CREDIT_WATERMARK = (
    '<div id="dcf-credit" style="position:fixed;bottom:10px;right:15px;'
    'font-size:0.75rem;color:rgba(255,255,255,0.4);pointer-events:none;'
    'z-index:9999;font-family:\'Inter\',sans-serif;opacity:0.6;">'
    f'Developed by {__author__} ({__roll__})</div>'
)

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

class AuthorCreditMiddleware(BaseHTTPMiddleware):
    """Injects author credit watermark into every HTML response."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return response
        # Read the response body
        body_chunks = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                body_chunks.append(chunk)
            else:
                body_chunks.append(chunk.encode("utf-8"))
        body = b"".join(body_chunks)
        html = body.decode("utf-8", errors="replace")
        # Inject credit before </body> if not already present
        if "dcf-credit" not in html and "</body>" in html:
            html = html.replace("</body>", _CREDIT_WATERMARK + "\n</body>")
        # Strip content-length so it's recalculated for the modified body
        new_headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return StarletteResponse(
            content=html,
            status_code=response.status_code,
            headers=new_headers,
            media_type="text/html"
        )

app.add_middleware(AuthorCreditMiddleware)


# IP utility functions

def normalize_ip(ip: str) -> str:
    """Clean up an IP address string.
    
    Handles edge cases like:
    - IPv6-mapped IPv4 addresses (::ffff:10.21.224.43 → 10.21.224.43)
    - IPv6 loopback (::1 → 127.0.0.1)
    - Whitespace and empty strings
    """
    if not ip:
        return "127.0.0.1"
    ip = ip.strip()
    if ip.startswith("::ffff:"):
        ip = ip.replace("::ffff:", "")
    if ip == "::1" or ip == "localhost":
        return "127.0.0.1"
    return ip

def is_valid_ip_or_pattern(val: str) -> bool:
    """Validate an IP address or subnet pattern for the whitelist."""
    val = val.strip()
    if not val:
        return False
    if val == "*":
        return True
    
    # each segment must be a valid octet (0-255) or the wildcard '*'
    parts = val.split('.')
    if len(parts) > 4:
        return False
        
    for part in parts:
        if part == '*':
            continue
        if not part.isdigit():
            return False
        if not 0 <= int(part) <= 255:
            return False
    return True

def get_local_ip():
    """Detect this machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    return normalize_ip(ip)

# Load the error page template once at import time.
_error_template = ""
try:
    error_path = "templates/error.html"
    if not os.path.exists(error_path):
        error_path = "error.html"
    
    with open(error_path, "r", encoding="utf-8") as _f:
        _error_template = _f.read()
except FileNotFoundError:
    pass

def render_error_page(status_code: int, title: str, detail: str, client_ip: str = "") -> HTMLResponse:
    """Render a pretty error page using our template."""
    if not _error_template:
        return HTMLResponse(content=f"<h1>{status_code} — {title}</h1><p>{detail}</p>", status_code=status_code)
    html = _error_template.replace("{{ERROR_CODE}}", str(status_code))
    html = html.replace("{{ERROR_TITLE}}", title)
    html = html.replace("{{ERROR_DETAIL}}", detail)
    html = html.replace("{{CLIENT_IP}}", client_ip or "unknown")
    return HTMLResponse(content=html, status_code=status_code)

# Custom error handlers
@app.exception_handler(404)
async def not_found_exception_handler(request: Request, exc: Exception):
    """Custom 404 page — shows HTML for browsers, JSON for curl/API."""
    client_ip = getattr(request.state, "client_ip", get_client_ip(request))
    is_curl = "curl" in request.headers.get("user-agent", "").lower()
    is_api = request.url.path.startswith("/api/") or request.url.path.startswith("/submit") or request.url.path.startswith("/starter/")
    if is_curl or is_api:
        return JSONResponse(status_code=404, content={"error": f"Route not found: {request.url.path}"})
        
    return render_error_page(404, "Page Not Found", "The page or resource you requested does not exist on this server.", client_ip)

@app.exception_handler(500)
async def internal_error_exception_handler(request: Request, exc: Exception):
    """Custom 500 page — something went wrong internally."""
    client_ip = getattr(request.state, "client_ip", get_client_ip(request))
    return render_error_page(500, "Internal Server Error", "An unexpected error occurred while processing your request.", client_ip)

from fastapi.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Catch-all for any HTTPException (403, 400, etc.)."""
    client_ip = getattr(request.state, "client_ip", get_client_ip(request))
    is_curl = "curl" in request.headers.get("user-agent", "").lower()
    
    # The double-slash paths (//submit, //starter) are here to handle edge cases
    # where a student's script might accidentally double the slash.
    is_api_or_submit = request.url.path.startswith("/api/") or request.url.path.startswith("/submit") or request.url.path.startswith("/starter/") or request.url.path.startswith("//submit") or request.url.path.startswith("//starter") or request.url.path.startswith("//task-status") or request.url.path.startswith("/task-status")
    
    if is_curl or is_api_or_submit:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
        
    return render_error_page(
        status_code=exc.status_code,
        title=f"Error {exc.status_code}",
        detail=str(exc.detail),
        client_ip=client_ip
    )

def is_authorized_system(client_ip: str, allowed_set: set) -> bool:
    """Check if an IP is authorized via system grants."""
    client_ip = normalize_ip(client_ip)
    if "*" in allowed_set:
        # Wildcard: ONLY allow IPs from LAB_SUBNETS
        if client_ip and any(client_ip.startswith(subnet + ".") for subnet in LAB_SUBNETS):
            return True
    
    if not client_ip:
        return False
    
    if client_ip in allowed_set:
        return True
    
    parts = client_ip.split('.')
    if len(parts) == 4:
        # 2nd octet wildcard: "22.*" → 10.22.*.*
        if f"{parts[1]}.*" in allowed_set:
            return True
        # 3rd octet wildcard: "225.*" (Legacy support for admin buttons)
        if f"{parts[2]}.*" in allowed_set:
            return True
        # 2nd+3rd octet wildcard: "22.38.*" → 10.22.38.*
        if f"{parts[1]}.{parts[2]}.*" in allowed_set:
            return True
        # 3rd+4th octet exact match: "224.72" → 10.*.224.72
        if f"{parts[2]}.{parts[3]}" in allowed_set:
            return True
        # 2nd+3rd+4th octet (full IP minus 10.): "22.38.139" → 10.22.38.139
        if f"{parts[1]}.{parts[2]}.{parts[3]}" in allowed_set:
            return True
        # Full dotted prefix: "10.22" → same as "22.*"
        if f"{parts[0]}.{parts[1]}" in allowed_set:
            return True
        # Full dotted prefix: "10.22.38" → same as "22.38.*"
        if f"{parts[0]}.{parts[1]}.{parts[2]}" in allowed_set:
            return True
        # Full dashed wildcard: "10.21.225.*"
        if f"{parts[0]}.{parts[1]}.{parts[2]}.*" in allowed_set:
            return True
            
    return False

def get_client_ip(request: Request) -> str:
    """Get the real client IP for this request."""
    if hasattr(request.state, "client_ip"):
        return request.state.client_ip

    real_ip = normalize_ip(request.client.host if request.client else "127.0.0.1")
    
    # Treat localhost requests as coming from the server itself (admin)
    if real_ip == "127.0.0.1" or real_ip == SERVER_IP:
        real_ip = SERVER_IP
        
    return real_ip

async def record_violation(v_type: str, roll_no: str, expected_ip: str, actual_ip: str):
    """Log a security violation to the violations CSV."""
    expected_ip = normalize_ip(expected_ip)
    actual_ip = normalize_ip(actual_ip)
    async with file_lock:
        rows = []
        found = False
        
        if os.path.exists(VIOLATIONS_FILE):
            with open(VIOLATIONS_FILE, mode='r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header or len(header) < 6:
                    header = ["timestamp", "violation_type", "roll_no", "expected_ip", "actual_ip", "count"]
                rows.append(header)
                
                for row in reader:
                    if len(row) >= 5:
                        r_time, r_type, r_roll, r_exp, r_act = row[0], row[1], row[2], row[3], row[4]
                        r_count = int(row[5]) if len(row) > 5 else 1
                        
                        # same violation from same student on same IP — just bump the count
                        if r_type == v_type and r_roll == roll_no and r_act == actual_ip:
                            r_count += 1
                            r_time = datetime.now().isoformat()
                            found = True
                        rows.append([r_time, r_type, r_roll, r_exp, r_act, str(r_count)])
        else:
            rows.append(["timestamp", "violation_type", "roll_no", "expected_ip", "actual_ip", "count"])
            
        if not found:
            rows.append([datetime.now().isoformat(), v_type, roll_no, expected_ip, actual_ip, "1"])
            
        with open(VIOLATIONS_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(rows)
    
    # Push a notification to any admin browser watching the violations page (SSE)
    for queue in violation_listeners:
        await queue.put("update")

# Access check middleware (security gatekeeper)
@app.middleware("http")
async def check_access(request: Request, call_next):
    # Step 1: Figure out who's connecting.
    raw_client_ip = normalize_ip(request.client.host if request.client else "127.0.0.1")
    
    # The admin is the person sitting at the server machine itself.
    # We also trust Parallels VM host IP (10.211.55.2) since the server
    # might run inside a VM with the admin using the host browser.
    is_real_admin = (
        raw_client_ip == "127.0.0.1" or 
        raw_client_ip == SERVER_IP 
    )
    
    # Admin testing feature: the admin can send an X-Lab-Test-IP header to
    # simulate what a specific student's IP would see. This is handy for
    # debugging "why can't student CS24B001 access the server?" without
    # walking over to their machine.
    if is_real_admin:
        test_ip = request.headers.get("X-Lab-Test-IP")
        if test_ip:
            client_ip = normalize_ip(test_ip)
        else:
            client_ip = SERVER_IP if raw_client_ip == "127.0.0.1" else raw_client_ip
    else:
        client_ip = raw_client_ip

    # Stash the resolved IP on the request so route handlers can access it
    request.state.client_ip = client_ip
    
    # Admin = the (possibly mocked) IP matches the server's own IP
    is_admin = (client_ip == SERVER_IP)
    request.state.is_admin = is_admin
    
    request_path = request.url.path
    
    # When the admin visits the homepage, send them straight to the admin panel
    if request_path == "/" and is_admin:
        return RedirectResponse(url="/admin", status_code=302)
    
    # 2. Admin-only routes
    admin_routes = (
        request_path.startswith("/admin") or 
        request_path == "/violations" or
        request_path.startswith("/system-access") or
        request_path.startswith("/api/system-access")
    )
    if admin_routes:
        if not is_admin:
            return render_error_page(403, "Access Denied", "Admin access is restricted.", client_ip)
        return await call_next(request)
    
    # 2. public pages: static resources, docs, and server status check
    public_paths = (
        request_path == "/api/server-status" or
        request_path.startswith("/docs") or
        request_path.startswith("/cppref") or
        request_path.startswith("/offline_files/") or
        request_path.startswith("/assets/") or
        request_path == "/api/offline-files" or
        request_path == "/favicon.ico" or
        request_path.startswith("/leaderboard") or
        request_path.startswith("/api/leaderboard") or
        request_path.startswith("/api/questions") or
        request_path.startswith("/status") or
        request_path.startswith("/api/status") or
        request_path.startswith("/api/request-recovery") or
        request_path.startswith("/api/recovery-status")
    )
    if public_paths:
        return await call_next(request)

    # 3. Hard IP gate for starter kit downloads and submissions
    #    These routes MUST come from LAB_SUBNETS. Students cannot bypass this
    #    from an outside network even if their IP is manually whitelisted.
    restricted_paths = (
        request_path.startswith("/starter/") or
        request_path.startswith("/submit")
    )
    is_lab_network = any(client_ip.startswith(subnet + ".") for subnet in LAB_SUBNETS)
    is_manually_authorized = is_authorized_system(client_ip, allowed_systems)

    if restricted_paths and not is_admin:
        if not is_lab_network:
            # Attempt to extract roll for violation logging
            path_parts = [p for p in request_path.split("/") if p]
            if request_path.startswith("/starter/") and len(path_parts) >= 2:
                roll_guess = path_parts[-1].upper().split('.')[0]
            elif request_path.startswith("/submit"):
                try:
                    form = await request.form()
                    roll_guess = form.get("roll") or "Unknown"
                except:
                    roll_guess = "Unknown"
            else:
                roll_guess = "Unknown"

            v_type = "Outside Network Submission" if request_path.startswith("/submit") else "Outside Network Download"
            # Only log violations for identifiable students, skip "Unknown" (browser visits, bots, etc.)
            if roll_guess != "Unknown":
                reg_data = ip_roll_map.get(roll_guess.upper())
                expected_ip = reg_data["ip"] if reg_data else "NA"
                await record_violation(v_type, roll_guess, expected_ip, client_ip)

            msg = "Your connection is outside the authorized lab network. Starter kit downloads and submissions are restricted to the DCF Lab LAN only."
            is_curl = "curl" in request.headers.get("user-agent", "").lower()
            wants_html = "text/html" in request.headers.get("accept", "").lower()
            if is_curl or not wants_html:
                return JSONResponse(status_code=403, content={"response": msg})
            return render_error_page(403, "Access Denied - Violation Recorded", msg, client_ip)

    # 4. submissions / starter kits: lab network + whitelist check
    
    # ALWAYS allow server IP / admins for all pages
    if is_admin:
        return await call_next(request)
        
    is_authorized = is_authorized_system(client_ip, allowed_systems)
    if is_authorized:
        return await call_next(request)

    # 5. Handle Unauthorized Access (in lab network but server closed, or other routes)
    
    # Try to grab roll from URL path for logging purposes
    parts = [p for p in request_path.split("/") if p]
    is_roll_in_path = (
        request_path.startswith("/starter/") or
        request_path.startswith("/api/recover/")
    )
    if is_roll_in_path and len(parts) >= 2:
        raw_roll = parts[-1].upper()
        roll_guess = raw_roll.split('.')[0]
    elif request_path.startswith("/submit"):
        try:
            form = await request.form()
            roll_guess = form.get("roll") or "Unknown"
        except:
            roll_guess = "Unknown"
    else:
        roll_guess = "Unknown"

    # Block access with a descriptive error
    is_curl = "curl" in request.headers.get("user-agent", "").lower()
    wants_html = "text/html" in request.headers.get("accept", "").lower()

    if is_lab_network:
        msg = "The server is currently closed. Please wait for the administrator to open it."
    else:
        msg = "Your connection is outside the authorized lab network. This system is restricted exclusively to the DCF Lab LAN."
        
    if is_curl or not wants_html:
        return JSONResponse(status_code=403, content={"response": msg})
        
    return render_error_page(403, "Access Denied", msg, client_ip)


# Page routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Student homepage — shows the exam interface."""
    if getattr(request.state, "is_admin", False):
        return RedirectResponse(url="/admin", status_code=302)
    try:
        return FileResponse("templates/index.html", media_type="text/html")
    except FileNotFoundError:
        return HTMLResponse(content="<h1>404 — index.html not found</h1>", status_code=404)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin dashboard — shows registered students, system access controls, etc."""
    course_name, lab_name = "Course", "Lab"
    try:
        real_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
        parts = real_path.split(os.sep)
        if len(parts) >= 3:
            lab_name = parts[-2]
            course_name = parts[-3]
    except Exception:
        pass

    try:
        return templates.TemplateResponse("admin.html", {
            "request": request,
            "course_name": course_name,
            "lab_name": lab_name
        })
    except Exception:
        return HTMLResponse(content="<h1>404 — admin.html not found</h1>", status_code=404)

@app.get("/system-access", response_class=HTMLResponse)
async def system_access_page():
    """System access management page — admin can whitelist/blacklist IPs here."""
    try:
        return FileResponse("templates/system_access.html", media_type="text/html")
    except FileNotFoundError:
        return HTMLResponse(content="<h1>404 — system_access.html not found</h1>", status_code=404)

# Admin API routes
def is_super_admin(request: Request) -> bool:
    """Check if the requester is an admin (i.e. connecting from the server machine)."""
    return getattr(request.state, "is_admin", False)

@app.get("/api/system-access")
async def get_system_access(request: Request):
    """Return the full system access state: who's registered, what's whitelisted.
    Also refreshes the in-memory registration map from disk in case of manual edits."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    
    is_super = is_super_admin(request)
    
    # Re-read registrations.csv every time this endpoint is called.
    # This lets the admin manually edit the CSV and see changes immediately.
    async with file_lock:
        ip_roll_map.clear()
        if os.path.exists(REGISTRATIONS_FILE):
            with open(REGISTRATIONS_FILE, mode='r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                for row in reader:
                    if len(row) >= 3 and row[0].strip():
                        ip_roll_map[row[0].strip().upper()] = {
                            "ip": row[1].strip(),
                            "timestamp": row[2].strip()
                        }

    return JSONResponse({
        "allowed_systems": list(allowed_systems),
        "registrations": ip_roll_map,
        "server_subnet": SERVER_SUBNET,
        "server_ip": SERVER_IP,
        "lab_subnets": LAB_SUBNETS,
        "is_super_admin": True
    })

@app.get("/api/is-admin")
async def check_is_admin(request: Request):
    """Simple check so the frontend can adapt its UI for admin vs student."""
    return JSONResponse({"is_admin": getattr(request.state, "is_admin", False)})

@app.get("/api/server-status")
async def get_server_status():
    """Public endpoint — returns whether the server is accepting submissions.
    Used by the 403 error page's auto-refresh to detect when the admin opens access."""
    return JSONResponse({"is_open": len(allowed_systems) > 0})

@app.get("/api/offline-files")
async def get_offline_files():
    """List downloadable offline resources (PDFs, notes, etc.) from the course directory."""
    offline_dir = ".active_lab/offline_files"
    docs_active = os.path.exists(".active_lab/cppreference")
    
    files = []
    if os.path.exists(offline_dir):
        try:
            files = [f for f in os.listdir(offline_dir) if os.path.isfile(os.path.join(offline_dir, f)) and not f.startswith('.')]
        except Exception as e:
            print(f"Error listing offline files: {e}")
    
    return JSONResponse(content={
        "files": files,
        "cppref_available": docs_active
    })

@app.get("/docs", response_class=HTMLResponse)
async def docs_page():
    """Offline documentation hub — links to cppreference, PDFs, etc."""
    try:
        return FileResponse("templates/docs_hub.html", media_type="text/html")
    except FileNotFoundError:
        return HTMLResponse(content="<h1>404 — docs_hub.html not found</h1>", status_code=404)

@app.get("/cppref", include_in_schema=False)
async def cppref_redirect():
    """Convenience redirect: /cppref → offline index page."""
    docs_path = ".active_lab/cppreference"
    if not os.path.exists(docs_path):
        return render_error_page(404, "Documentation Not Available", "The offline C++ documentation files were not found on the server.")
    
    # The index is commonly at /reference/en/index.html
    return RedirectResponse(url="/cppref/reference/en/index.html", status_code=302)

# Offline documentation — serves local files from .active_lab/cppreference
from fastapi.staticfiles import StaticFiles
DOCS_DIR = ".active_lab/cppreference"
if os.path.exists(DOCS_DIR):
    app.mount("/cppref", StaticFiles(directory=DOCS_DIR), name="cppref")
else:
    print(f"⚠️  Documentation directory missing: {DOCS_DIR}. Documentation links will be hidden.")


@app.post("/admin/grant_access")
async def grant_access(request: Request, roll: str):
    """Add a roll number to the authorized student list (students.txt)."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    roll_upper = roll.strip().upper()
    if not roll_upper:
        raise HTTPException(status_code=400, detail="Invalid roll number provided")
        
    if roll_upper in get_student_list():
        return {"status": "Already authorized"}
    
    with open(STUDENTS_FILE, "a") as f:
        f.write(f"\n{roll_upper}")
        
    return {"status": "Success", "roll": roll_upper}

def save_allowed_systems():
    """Persist the current whitelist to disk so it survives server restarts."""
    with open(SYSTEM_ACCESS_FILE, 'w', encoding='utf-8') as f:
        for sys in sorted(allowed_systems):
            f.write(sys + "\n")

@app.post("/admin/grant_system")
async def grant_system(request: Request, system: str):
    """Grant access to a system by IP pattern.
    
    Accepts flexible patterns like:
      '*'          → open to ALL lab machines
      '224.*'      → open to all IPs in 10.21.224.*
      '225.71'     → open to one specific machine
    
    When '*' is granted, all previous specific rules are cleared since the
    wildcard already covers everything.
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    num = system.strip()
    if not is_valid_ip_or_pattern(num):
        raise HTTPException(status_code=400, detail="Invalid system number or pattern")
    
    # remove duplicates and replace with the new rule
    if num in allowed_systems:
        allowed_systems.remove(num)
        
    if num == "*":
        allowed_systems.clear()  # wildcard supersedes all specific rules
        
    allowed_systems.add(num)
    save_allowed_systems()
        
    return {"status": "Success", "system": num}

@app.post("/admin/revoke_system")
async def revoke_system(request: Request, system: str):
    """Revoke a previously granted system access rule.
    
    If the exact pattern isn't in the whitelist, we check if a broader rule
    (like '*') is granting access and tell the admin which rule to revoke instead.
    This prevents confusion like "I revoked 224.71 but it still works!" —
    because wildcard '*' was still active.
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    num = system.strip()
    if not num:
        raise HTTPException(status_code=400, detail="Invalid system number")
    
    # Revoking '*' means closing the server entirely (clearing all access)
    if num == "*":
        count = len(allowed_systems)
        if count == 0:
            return {"status": "Already revoked"}
        bindings = len(ip_roll_map)
        allowed_systems.clear()
        with open(SYSTEM_ACCESS_FILE, mode='w', encoding='utf-8') as f:
            f.write("")
        return {"status": f"Revoked all ({bindings} bindings)"}
    
    if num in allowed_systems:
        allowed_systems.discard(num)
        # save
        with open(SYSTEM_ACCESS_FILE, mode='w', encoding='utf-8') as f:
            f.write("\n".join(allowed_systems))
        return {"status": "Revoked", "system": num}
    
    # not directly in the set, but might still be covered by a broader rule
    # tell the user which rule is actually granting access
    is_still_granted = False
    granting_rule = None
    
    # check broader rules
    if "*" in allowed_systems:
        is_still_granted = True
        granting_rule = "*"
    else:
        # match against octet-level rules
        parts = num.split('.')
        if len(parts) == 4:
            last = parts[3]
            sub_wild = f"{parts[2]}.*"
            sub_last = f"{parts[2]}.{parts[3]}"
            if last in allowed_systems:
                is_still_granted = True
                granting_rule = last
            elif sub_wild in allowed_systems:
                is_still_granted = True
                granting_rule = sub_wild
            elif sub_last in allowed_systems:
                is_still_granted = True
                granting_rule = sub_last
    
    if is_still_granted:
        return {
            "status": "Still Authorized", 
            "detail": f"System '{num}' is covered by active rule '{granting_rule}'. Click '{granting_rule}' below to revoke it."
        }
    
    return {"status": "Not found", "detail": "No matching rule found in whitelist."}

@app.get("/api/roll-prefixes")
async def get_roll_prefixes(request: Request):
    """Return unique roll number prefixes detected from students.txt (e.g., CS24B, CS23B)."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")
    import re
    prefixes = set()
    for roll in get_student_list():
        # Extract everything except the last 3 digits
        m = re.match(r'^(.+?)(\d{3})$', roll)
        if m:
            prefixes.add(m.group(1))
        else:
            prefixes.add(roll)  # fallback: whole roll as prefix
    return {"prefixes": sorted(prefixes)}

@app.get("/api/student-list")
async def api_student_list(request: Request):
    """Return the entire list of authorized students for admin autocomplete."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")
    return {"students": get_student_list()}

@app.post("/admin/delete_registration")
async def delete_registration(request: Request, roll: str = None, clear_all: bool = False):
    """Unlink a student's PC registration so they can re-register from a different machine.
    
    clear_all=true wipes every registration (useful for resetting between lab sessions).
    Individual deletions just remove one student's IP binding.
    Either way, their past submissions stay on disk — only the IP link is removed.
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    async with file_lock:
        if clear_all:
            count = len(ip_roll_map)
            ip_roll_map.clear()
            # reset CSV
            with open(REGISTRATIONS_FILE, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["roll_no", "ip_address", "timestamp"])
            return {"status": f"Cleared all {count} registration(s)."}

        if not roll:
            raise HTTPException(status_code=400, detail="Provide a roll number or set clear_all=true")
        
        roll_upper = roll.strip().upper()
        if roll_upper not in ip_roll_map:
            return {"status": f"No registration found for {roll_upper}."}
        
        del ip_roll_map[roll_upper]
        
        # rewrite CSV without this entry
        with open(REGISTRATIONS_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["roll_no", "ip_address", "timestamp"])
            for r_no, data in ip_roll_map.items():
                if isinstance(data, dict):
                    writer.writerow([r_no, data.get("ip", ""), data.get("timestamp", "")])
                else:
                    writer.writerow([r_no, data, ""])
        
        return {"status": f"Deleted registration for {roll_upper}."}



@app.get("/admin/violations")
async def get_violations():
    """Return all recorded violations as a JSON array for the admin violations page."""
    if not os.path.exists(VIOLATIONS_FILE):
        return JSONResponse(content=[])
    
    rows = []
    with open(VIOLATIONS_FILE, mode='r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return JSONResponse(content=rows)

@app.get("/api/violations-count")
async def get_violations_count():
    """Return the total number of security violations logged."""
    if not os.path.exists(VIOLATIONS_FILE):
        return JSONResponse({"count": 0})
    
    try:
        with open(VIOLATIONS_FILE, mode='r', encoding='utf-8') as f:
            # subtract 1 for the header row
            count = sum(1 for _ in f) - 1
            return JSONResponse({"count": max(0, count)})
    except Exception:
        return JSONResponse({"count": 0})

@app.get("/admin/violation-events")
async def violation_events(request: Request):
    """Server-Sent Events (SSE) endpoint for real-time violation updates.
    
    The admin violations page opens a persistent connection here. Whenever
    record_violation() logs a new violation, it pushes 'update' to every
    connected queue, and the browser gets an instant notification to refresh
    the violations table — no polling needed.
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    
    queue = asyncio.Queue()
    violation_listeners.append(queue)
    
    async def event_generator():
        try:
            while True:
                # wait for a notification
                await queue.get()
                yield "data: update\n\n"
        except asyncio.CancelledError:
            if queue in violation_listeners:
                violation_listeners.remove(queue)
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/admin/delete_violation")
async def delete_violation(request: Request, roll: str = None, v_type: str = None, clear_all: bool = False):
    """Delete violations from the log.
    
    Supports clearing all violations, or targeting a specific student
    (optionally filtered by violation type). Useful for pardoning false positives.
    """
    if not is_super_admin(request):
        raise HTTPException(status_code=403, detail="Super-admin privileges required")
    async with file_lock:
        if clear_all:
            if os.path.exists(VIOLATIONS_FILE):
                with open(VIOLATIONS_FILE, mode='w', encoding='utf-8') as f:
                    f.write("timestamp,violation_type,roll_no,expected_ip,actual_ip,count\n")
            return JSONResponse({"status": "All violations cleared."})

        if not roll:
            return JSONResponse(status_code=400, content={"detail": "Provide roll, or set clear_all=true."})

        roll_upper = roll.strip().upper()
        v_type_filter = v_type.strip() if v_type else None

        if not os.path.exists(VIOLATIONS_FILE):
            return JSONResponse({"status": "No violations file found."})

        kept_rows = []
        removed = 0
        with open(VIOLATIONS_FILE, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or ["timestamp", "violation_type", "roll_no", "expected_ip", "actual_ip", "count"]
            for row in reader:
                match_roll = row.get("roll_no", "").upper() == roll_upper
                match_type = (v_type_filter is None) or (row.get("violation_type", "") == v_type_filter)
                if match_roll and match_type:
                    removed += 1
                else:
                    kept_rows.append(row)

        with open(VIOLATIONS_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept_rows)

        scope = f" (type: {v_type_filter})" if v_type_filter else ""
        return JSONResponse({"status": f"Removed {removed} violation(s) for {roll_upper}{scope}. {len(kept_rows)} remaining."})

@app.post("/admin/delete_submission")
async def delete_submission(request: Request, roll: str, qno: Optional[str] = None):
    """Delete a student's submission files and update their grades.
    
    Can target specific questions (comma-separated) or wipe all questions.
    Also recalculates grades.csv and busts the leaderboard cache so rankings
    update instantly. Use this when a student needs a fresh attempt.
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    roll_upper = roll.strip().upper()
    if not roll_upper or not roll_upper.replace('_','').isalnum():
        raise HTTPException(status_code=400, detail="Invalid roll number")
    
    base_submission_dir = os.path.join(".active_lab", "submissions")
    deleted_count = 0
    deleted_questions = []
    if os.path.isdir(base_submission_dir):
        import shutil
        # support comma-separated question list
        target_questions = [q.strip().upper() for q in qno.split(",") if q.strip()] if qno else None
        
        roll_dir = os.path.join(base_submission_dir, roll_upper)
        if os.path.isdir(roll_dir):
            q_dirs = target_questions if target_questions else [
                d for d in os.listdir(roll_dir) if os.path.isdir(os.path.join(roll_dir, d))
            ]
            for q in q_dirs:
                target_dir = os.path.join(roll_dir, q)
                if os.path.exists(target_dir):
                    try:
                        shutil.rmtree(target_dir)
                        deleted_count += 1
                        deleted_questions.append(q)
                        # bust leaderboard cache
                        if q in _LEADERBOARD_CACHE:
                            del _LEADERBOARD_CACHE[q]
                    except Exception as e:
                        return JSONResponse(status_code=500, content={"status": f"Error deleting {q}: {str(e)}"})
    
    # update grades.csv
    if deleted_count > 0:
        grades_file = os.path.join(".active_lab", "grades.csv")
        registrations_file = os.path.join(".active_lab", "registrations.csv")
        
        # check who is registered so we pick 0.0 vs Absent
        registered_students = set()
        if os.path.exists(registrations_file):
            with open(registrations_file, "r", newline="", encoding="utf-8") as rf:
                rdr = csv.DictReader(rf)
                for row in rdr:
                    if row.get("roll_no"):
                        registered_students.add(row["roll_no"].upper())
                        
        if os.path.exists(grades_file):
            try:
                kept_rows = []
                with open(grades_file, mode='r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames or []
                    for row in reader:
                        if row.get("roll", "").upper() == roll_upper:
                            is_registered = roll_upper in registered_students
                            default_grade = "0.0" if is_registered else "Absent"
                            
                            if qno:
                                # clear only the specified questions
                                for q in deleted_questions:
                                    if q in row:
                                        row[q] = default_grade
                            else:
                                # clear everything
                                for key in row:
                                    if key != "Total" and key != "roll":
                                        row[key] = default_grade
                                        
                            # Recalculate Total
                            if "Total" in row:
                                total_score = 0.0
                                for key, val in row.items():
                                    if key.startswith("Q") and val and val != "Absent":
                                        try:
                                            total_score += float(val)
                                        except ValueError:
                                            pass
                                row["Total"] = str(round(total_score, 2))
                                
                        kept_rows.append(row)
                
                with open(grades_file, mode='w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(kept_rows)
                    
                # Invalidate the entire leaderboard cache so it recalculates instantly
                _LEADERBOARD_CACHE.clear()
            except Exception:
                pass  # Best effort — submission was still deleted
                        
    if deleted_count > 0:
        scope = f"for question {qno.upper()}" if qno else f"across {deleted_count} question(s)"
        return {"status": f"Deleted submissions for {roll_upper} {scope}. Leaderboard and rankings updated."}
    else:
        scope = f" for question {qno.upper()}" if qno else ""
        return {"status": f"No submissions found for {roll_upper}{scope}."}

@app.post("/admin/run-moss")
async def run_moss(request: Request):
    """Run Stanford MOSS on student submissions."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    
    data = await request.json()
    qno = data.get("question", "").strip()
    moss_id = data.get("moss_id", "").strip()
    
    if not moss_id:
        return JSONResponse(status_code=400, content={"status": "Error: MOSS ID is required"})
        
    import mosspy, asyncio
    
    # Resolve metadata for report title
    course_name, lab_name = "Course", "Lab"
    try:
        sub_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
        parts = sub_path.split("/")
        if "courses" in parts:
            idx = parts.index("courses")
            if len(parts) > idx + 2:
                course_name, lab_name = parts[idx + 1], parts[idx + 2]
    except: pass
    
    base_submission_dir = os.path.join(".active_lab", "submissions")
    
    # Handle 'All' by finding all question directories across all students
    is_all = qno.lower() == "all" or not qno
    if is_all:
        q_set = set()
        for roll in os.listdir(base_submission_dir):
            roll_path = os.path.join(base_submission_dir, roll)
            if os.path.isdir(roll_path):
                for d in os.listdir(roll_path):
                    if os.path.isdir(os.path.join(roll_path, d)):
                        q_set.add(d)
        q_dirs = sorted(q_set)
    else:
        q_dirs = [qno.upper()]
        
    generated_reports = []
    
    for idx, q in enumerate(q_dirs):
        # Prevent self-comparison by running a fresh MOSS instance per question
        m = mosspy.Moss(moss_id, "cc")
        m.setCommentString(f"{course_name} - {lab_name} - {q}")
        
        cpp_files_found = 0
        for roll in os.listdir(base_submission_dir):
            roll_path = os.path.join(base_submission_dir, roll)
            if not os.path.isdir(roll_path): continue
            
            q_path = os.path.join(roll_path, q)
            if not os.path.isdir(q_path): continue
                
            # Pick best/latest submission
            marks_path = os.path.join(q_path, "marks.txt")
            best_ts, best_mark = None, -1.0
            if os.path.exists(marks_path):
                try:
                    with open(marks_path, "r") as mf:
                        for line in mf:
                            m_parts = line.strip().split(',')
                            if len(m_parts) >= 2:
                                try:
                                    ts, mark = m_parts[0].strip(), float(m_parts[1].strip())
                                    if mark > best_mark:
                                        best_mark, best_ts = mark, ts
                                except: pass
                except: pass
            
            ts_dirs = sorted([d for d in os.listdir(q_path) if os.path.isdir(os.path.join(q_path, d))], reverse=True)
            target_dir = next((ts for ts in ts_dirs if best_ts and (best_ts.replace('-', '').replace(' ', '-').replace(':', '') in ts or ts in best_ts)), ts_dirs[0] if ts_dirs else None)
                
            if target_dir:
                ts_path = os.path.join(q_path, target_dir)
                cpps = [f for f in os.listdir(ts_path) if f.endswith(".cpp")]
                if cpps:
                    m.addFile(os.path.join(ts_path, cpps[0]), f"{q}/{roll}.cpp")
                    cpp_files_found += 1
                    
        if cpp_files_found >= 2:
            try:
                # Rate limit protection (3s between questions if multiple)
                if idx > 0: await asyncio.sleep(3)
                
                url = m.send()
                generated_reports.append({"q": q, "url": url})
                
                # Log to Course History (formatted date time)
                try:
                    real_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
                    course_dir = os.path.dirname(os.path.dirname(real_path))
                    history_file = os.path.join(course_dir, "moss_history.csv")
                    from datetime import datetime
                    import csv
                    file_exists = os.path.isfile(history_file)
                    with open(history_file, "a", newline="") as h_f:
                        writer = csv.writer(h_f)
                        if not file_exists:
                            writer.writerow(["timestamp", "lab", "question", "url"])
                        timestamp = datetime.now().strftime("%d %B %Y %I:%M %p")
                        writer.writerow([timestamp, lab_name, q, url])
                except: pass
            except Exception as e:
                return JSONResponse(status_code=500, content={"status": f"MOSS Error: {str(e)}. (Hint: Check if Outbound Port 7690 is blocked.)"})
    
    if not generated_reports:
        return JSONResponse(status_code=400, content={"status": "Error: Found insufficient submissions (need at least 2 per question)."})
    
    if is_all:
        return JSONResponse({"status": "Success", "message": f"Generated reports for {len(generated_reports)} questions. Check MOSS History for links."})
    else:
        # For single question runs, return the direct URL for opening in a new tab
        return JSONResponse({"status": "Success", "report_url": f"/admin/moss-proxy?url={generated_reports[0]['url']}"})

@app.post("/admin/moss-colab")
async def generate_moss_colab(request: Request):
    """Generate a Google Colab notebook with all submissions embedded for MOSS.
    
    This is a fallback for when port 7690 is blocked by the lab firewall.
    The notebook contains all student .cpp files base64-encoded, and a script
    that decodes them, runs mosspy, and prints the report URL.
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    
    import base64
    
    data = await request.json()
    qno = data.get("question", "").strip()
    moss_id = data.get("moss_id", "").strip()
    
    if not moss_id:
        return JSONResponse(status_code=400, content={"status": "Error: MOSS ID is required"})
    
    # Resolve course/lab names
    course_name, lab_name = "Course", "Lab"
    try:
        sub_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
        parts = sub_path.split("/")
        if "courses" in parts:
            idx = parts.index("courses")
            if len(parts) > idx + 2:
                course_name, lab_name = parts[idx + 1], parts[idx + 2]
    except: pass
    
    base_submission_dir = os.path.join(".active_lab", "submissions")
    
    is_all = qno.lower() == "all" or not qno
    if is_all:
        q_set = set()
        for roll in os.listdir(base_submission_dir):
            roll_path = os.path.join(base_submission_dir, roll)
            if os.path.isdir(roll_path):
                for d in os.listdir(roll_path):
                    if os.path.isdir(os.path.join(roll_path, d)):
                        q_set.add(d)
        q_dirs = sorted(q_set)
    else:
        q_dirs = [qno.upper()]
    
    # Collect all .cpp files as {display_name: base64_content}
    all_files = {}
    
    for q in q_dirs:
        for roll in os.listdir(base_submission_dir):
            roll_path = os.path.join(base_submission_dir, roll)
            if not os.path.isdir(roll_path): continue
            
            q_path = os.path.join(roll_path, q)
            if not os.path.isdir(q_path): continue
            
            # Same best-submission logic as run-moss
            marks_path = os.path.join(q_path, "marks.txt")
            best_ts, best_mark = None, -1.0
            if os.path.exists(marks_path):
                try:
                    with open(marks_path, "r") as mf:
                        for line in mf:
                            m_parts = line.strip().split(',')
                            if len(m_parts) >= 2:
                                try:
                                    ts, mark = m_parts[0].strip(), float(m_parts[1].strip())
                                    if mark > best_mark:
                                        best_mark, best_ts = mark, ts
                                except: pass
                except: pass
            
            ts_dirs = sorted([d for d in os.listdir(q_path) if os.path.isdir(os.path.join(q_path, d))], reverse=True)
            target_dir = next((ts for ts in ts_dirs if best_ts and (best_ts.replace('-', '').replace(' ', '-').replace(':', '') in ts or ts in best_ts)), ts_dirs[0] if ts_dirs else None)
            
            if target_dir:
                ts_path = os.path.join(q_path, target_dir)
                cpps = [f for f in os.listdir(ts_path) if f.endswith(".cpp")]
                if cpps:
                    filepath = os.path.join(ts_path, cpps[0])
                    display_name = f"{q}/{roll}.cpp"
                    try:
                        with open(filepath, "r", encoding="utf-8", errors="replace") as cf:
                            content = cf.read()
                        all_files[display_name] = base64.b64encode(content.encode("utf-8")).decode("ascii")
                    except: pass
    
    if len(all_files) < 2:
        return JSONResponse(status_code=400, content={"status": "Error: Need at least 2 submissions to generate notebook."})
    
    # Build the file-decoding Python code for the notebook
    files_dict_lines = []
    for name, b64 in all_files.items():
        files_dict_lines.append(f'    "{name}": "{b64}",')
    files_dict_str = "\n".join(files_dict_lines)
    
    q_label = qno.upper() if not is_all else "All"
    title = f"{course_name} - {lab_name} - {q_label}"
    
    # Build notebook cells
    cells = [
        # Cell 1: Title
        _nb_markdown_cell(f"# MOSS Plagiarism Check — {title}\n\nThis notebook was auto-generated by the DCF Exam Server.\\\n**Run all cells** (Runtime → Run all) to submit to MOSS and get the report URL."),
        # Cell 2: Install mosspy
        _nb_code_cell("!pip install mosspy -q\nprint('mosspy installed')"),
        # Cell 3: Decode files
        _nb_code_cell(f'import base64, os\n\nfiles = {{\n{files_dict_str}\n}}\n\nprint(f"Decoding {{len(files)}} student submissions...")\nfor name, b64 in files.items():\n    os.makedirs(os.path.dirname(name), exist_ok=True)\n    with open(name, "w") as f:\n        f.write(base64.b64decode(b64).decode("utf-8"))\nprint(f"{{len(files)}} files ready")'),
        # Cell 4: Run MOSS
        _nb_code_cell(f'import mosspy\nimport json\n\nmoss_id = "{moss_id}"\nquestions = {list(set(n.split("/")[0] for n in all_files.keys()))}\nall_urls = {{}}\n\nfor q in sorted(questions):\n    m = mosspy.Moss(moss_id, "cc")\n    m.setCommentString("{title} - " + q)\n    q_files = [f for f in files.keys() if f.startswith(q + "/")]\n    for f in q_files:\n        m.addFile(f, f)\n    url = m.send()\n    all_urls[q] = url\n\nprint(json.dumps(all_urls))'),
    ]
    
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 0,
        "metadata": {
            "colab": {"provenance": [], "name": f"{course_name}_{lab_name}_{q_label}.ipynb"},
            "kernelspec": {"name": "python3", "display_name": "Python 3"},
            "language_info": {"name": "python"}
        },
        "cells": cells
    }
    
    notebook_json = json.dumps(notebook, indent=2)
    filename = f"{course_name}_{lab_name}_{q_label}.ipynb"
    
    return Response(
        content=notebook_json,
        media_type="application/x-ipynb+json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


def _nb_markdown_cell(source: str) -> dict:
    """Create a Jupyter notebook markdown cell."""
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": [line + "\n" for line in source.split("\n")]
    }

def _nb_code_cell(source: str) -> dict:
    """Create a Jupyter notebook code cell."""
    return {
        "cell_type": "code",
        "metadata": {},
        "source": [line + "\n" for line in source.split("\n")],
        "execution_count": None,
        "outputs": []
    }


@app.post("/admin/moss-import-url")
async def import_moss_url(request: Request):
    """Import a MOSS report URL into the history.
    
    Used when the admin runs MOSS externally (e.g., via Colab notebook)
    and wants to save the report URL to the course's MOSS history.
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    
    data = await request.json()
    import re
    url_text = data.get("url", "").strip()
    fallback_q = data.get("question", "Manual").strip() or "Manual"
    
    reports_to_save = []
    
    # Check if bulk JSON format is pasted (e.g. {"Q1": "http://moss..."})
    bulk_matches = re.findall(r'"([^"]+)"\s*:\s*"(http://moss\.stanford\.edu/[^"]+)"', url_text)
    if bulk_matches:
        reports_to_save = [{"q": m[0], "u": m[1]} for m in bulk_matches]
    else:
        # Fallback to single URL parse
        single = re.search(r'(http://moss\.stanford\.edu/[^\s"]+)', url_text)
        if single:
            reports_to_save = [{"q": fallback_q, "u": single.group(1)}]
        else:
            return JSONResponse(status_code=400, content={"status": "Error: Please provide a valid MOSS report URL."})
    
    # Save to course history
    try:
        real_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
        course_dir = os.path.dirname(os.path.dirname(real_path))
        history_file = os.path.join(course_dir, "moss_history.csv")
        
        lab_name = "Lab"
        parts = real_path.split(os.sep)
        if len(parts) >= 2:
            lab_name = parts[-2]
        
        file_exists = os.path.isfile(history_file)
        with open(history_file, "a", newline="") as h_f:
            writer = csv.writer(h_f)
            if not file_exists:
                writer.writerow(["timestamp", "lab", "question", "url"])
            timestamp = datetime.now().strftime("%d %B %Y %I:%M %p")
            
            for rep in reports_to_save:
                writer.writerow([timestamp, lab_name, rep["q"].upper(), rep["u"]])
        
        msg = f"Saved URL for {reports_to_save[0]['q'].upper()}." if len(reports_to_save) == 1 else f"Bulk imported {len(reports_to_save)} MOSS URLs."
        return JSONResponse({"status": "Success", "message": msg})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": f"Error saving URL: {str(e)}"})

@app.get("/admin/moss-history")
async def get_moss_history(request: Request):
    """Retrieve course-global history of MOSS reports."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    
    try:
        real_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
        course_dir = os.path.dirname(os.path.dirname(real_path))
        history_file = os.path.join(course_dir, "moss_history.csv")
    except:
        return []

    if not os.path.exists(history_file):
        return []

    import csv
    reports = []
    try:
        with open(history_file, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                reports.append(row)
    except:
        pass
    return reports[::-1] # Most recent first

@app.delete("/admin/moss-history")
async def delete_moss_history_entry(request: Request, index: int):
    """Delete a specific MOSS history entry by its original row index (0-based)."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")

    try:
        real_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
        course_dir = os.path.dirname(os.path.dirname(real_path))
        history_file = os.path.join(course_dir, "moss_history.csv")
    except:
        return JSONResponse(status_code=404, content={"error": "History file not found"})

    if not os.path.exists(history_file):
        return JSONResponse(status_code=404, content={"error": "History file not found"})

    import csv as csv_mod
    rows = []
    try:
        with open(history_file, "r") as f:
            reader = csv_mod.reader(f)
            rows = list(reader)
    except:
        return JSONResponse(status_code=500, content={"error": "Failed to read history"})

    # rows[0] is the header, data rows start at index 1
    row_to_delete = index + 1  # offset by header
    if row_to_delete < 1 or row_to_delete >= len(rows):
        return JSONResponse(status_code=400, content={"error": "Invalid index"})

    rows.pop(row_to_delete)

    try:
        with open(history_file, "w", newline="") as f:
            writer = csv_mod.writer(f)
            writer.writerows(rows)
    except:
        return JSONResponse(status_code=500, content={"error": "Failed to update history"})

    return JSONResponse(content={"status": "deleted"})

@app.get("/admin/moss-history-page")
async def moss_history_page(request: Request):
    """Serve a dedicated HTML page for MOSS history."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    
    course_name = "Course"
    try:
        real_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
        parts = real_path.split(os.sep)
        if len(parts) >= 3:
            course_name = parts[-3]
    except:
        pass

    return templates.TemplateResponse("moss_history.html", {"request": request, "course_name": course_name})

@app.get("/admin/moss-proxy")
async def moss_proxy(request: Request, url: str):
    """Reverse proxy to Stanford MOSS to bypass iframe restrictions.
    
    Proxies the main results page, frameset comparison pages, and individual
    code-view frames — applying a modern dark theme to all of them.
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    from fastapi.responses import HTMLResponse
    import urllib.request, urllib.parse, re as _re

    def _proxy_url(target: str) -> str:
        """Rewrite a MOSS URL to go through our proxy."""
        return f"/admin/moss-proxy?url={urllib.parse.quote(target, safe='')}"

    def _rewrite_links(html: str, base_url: str) -> str:
        """Rewrite all href/src attributes pointing to moss.stanford.edu to go through our proxy."""
        from urllib.parse import urljoin
        def _replace_attr(m):
            attr = m.group(1)   # href or src
            quote = m.group(2)  # quote character
            raw = m.group(3)    # original URL
            # Resolve relative URLs
            full = urljoin(base_url, raw) if not raw.startswith('http') else raw
            if 'moss.stanford.edu' in full:
                return f'{attr}={quote}{_proxy_url(full)}{quote}'
            return m.group(0)
        return _re.sub(r'(href|src|SRC|HREF)=(["\'])([^"\']*)\2', _replace_attr, html)

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            content_type = response.headers.get('Content-Type', 'text/html')
            raw_data = response.read()

        # If not HTML (images, GIFs, etc.), return raw binary with correct content type
        if 'text/html' not in content_type:
            from fastapi.responses import Response
            return Response(content=raw_data, media_type=content_type)

        html = raw_data.decode('utf-8', errors='replace')

        # Detect page type
        is_frameset = '<frameset' in html.lower() or '<FRAMESET' in html

        # ── Rewrite all MOSS links/frames to go through our proxy ──
        html = _rewrite_links(html, url)

        if is_frameset:
            # Frameset pages: just rewrite frame sources (already done above) and return
            return HTMLResponse(content=html)

        elif 'How to Read the Results' not in html:
            # Any sub-page (code frames, match summaries, etc.) — keep default MOSS styling
            return HTMLResponse(content=html)

        else:
            # Main results table page — full modern styling
            modern_css = """
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap');
                :root {
                    --bg: #0f172a;
                    --card: #1e293b;
                    --text: #f8fafc;
                    --muted: #94a3b8;
                    --accent: #fbbf24;
                }
                body { 
                    background: var(--bg); 
                    color: var(--text); 
                    font-family: 'Inter', sans-serif; 
                    padding: 2rem;
                    max-width: 1200px;
                    margin: 0 auto;
                }
                table { 
                    width: 100%; 
                    border-collapse: separate; 
                    border-spacing: 0;
                    background: var(--card);
                    border-radius: 12px;
                    overflow: hidden;
                    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.3);
                    border: 1px solid rgba(255,255,255,0.05);
                    margin-bottom: 2rem;
                }
                th { 
                    background: rgba(255, 255, 255, 0.03); 
                    color: var(--muted);
                    text-transform: uppercase;
                    font-size: 0.75rem;
                    font-weight: 600;
                    letter-spacing: 0.05em;
                    padding: 1.25rem 1rem;
                    text-align: left;
                    border-bottom: 1px solid rgba(255,255,255,0.1);
                }
                td { 
                    padding: 1.25rem 1rem; 
                    border-bottom: 1px solid rgba(255,255,255,0.05);
                    font-size: 0.9rem;
                }
                tr:last-child td { border-bottom: none; }
                tr:hover td { background: rgba(255, 255, 255, 0.02); }
                a { 
                    color: var(--accent); 
                    text-decoration: none; 
                    font-weight: 500;
                }
                a:hover { text-decoration: underline; }
                h2, h3 { margin-bottom: 0.5rem; color: var(--text); }
                hr { border: none; border-top: 1px solid rgba(255,255,255,0.1); margin: 2rem 0; }
                p { color: var(--muted); }
                th:last-child, td:last-child {
                    text-align: right;
                    padding-right: 2rem;
                }
            </style>
            """
            html = modern_css + html

            # Remove the "[ How to Read the Results | Tips | ... | Credits ]" nav block + <hr>/<HR>
            html = _re.sub(r'\[[\s\S]*?How to Read the Results[\s\S]*?Credits[\s\S]*?\](\s*<hr>)?', '', html, flags=_re.IGNORECASE)
            # Remove legacy metadata lines
            html = _re.sub(r'^.*Options -l.*$', '', html, flags=_re.MULTILINE)
            html = _re.sub(r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+.*?\d{4}', '', html)
            
            return HTMLResponse(content=html)
    except Exception as e:
        return HTMLResponse(content=f"<h1>Error loading MOSS</h1><p>{str(e)}</p>")


@app.get("/admin/download_submissions")
async def download_submissions(request: Request):
    if not is_super_admin(request):
        raise HTTPException(status_code=403, detail="Super-admin privileges required")
    """Download all student submissions as a ZIP file, named with Course and Lab."""
    import zipfile
    import io
    import csv
    
    course_name = "Course"
    lab_name = "Lab"
    try:
        real_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
        parts = real_path.split(os.sep)
        if len(parts) >= 3:
            lab_name = parts[-2]
            course_name = parts[-3]
    except Exception:
        pass

    zip_filename = f"{course_name}_{lab_name}_Submissions.zip"
    memory_file = io.BytesIO()
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        submissions_dir = os.path.join(".active_lab", "submissions")
        
        # 1. Identify all questions from testcases (source of truth) + student subdirs
        questions = set()
        testcases_dir = os.path.join(".active_lab", "testcases")
        if os.path.isdir(testcases_dir):
            questions.update(d for d in os.listdir(testcases_dir) if os.path.isdir(os.path.join(testcases_dir, d)))
        if os.path.exists(submissions_dir):
            for roll in os.listdir(submissions_dir):
                roll_path = os.path.join(submissions_dir, roll)
                if os.path.isdir(roll_path):
                    questions.update(d for d in os.listdir(roll_path) if os.path.isdir(os.path.join(roll_path, d)))
        questions = sorted(questions)
        
        # 2. Build the dynamic grading report
        ordered_students = []
        if os.path.exists(STUDENTS_FILE):
             with open(STUDENTS_FILE, "r") as f:
                 ordered_students = [line.strip().upper() for line in f if line.strip()]
        else:
            ordered_students = sorted(list(get_student_list()))

        # registrations for 0.0 vs Absent logic
        registered_students = set()
        registrations_file = os.path.join(".active_lab", "registrations.csv")
        if os.path.exists(registrations_file):
            with open(registrations_file, "r", newline="", encoding="utf-8") as rf:
                rdr = csv.DictReader(rf)
                for r_row in rdr:
                    if r_row.get("roll_no"):
                        registered_students.add(r_row["roll_no"].upper())

        grades_data = []
        for s_roll in ordered_students:
            row = {"roll": s_roll}
            total_sum = 0.0
            has_submitted_any = False
            is_registered = s_roll in registered_students
            
            for q in questions:
                marks_path = os.path.join(submissions_dir, s_roll, q, "marks.txt")
                if os.path.exists(marks_path):
                    has_submitted_any = True
                    try:
                        with open(marks_path, "r") as mf:
                            max_marks = 0.0
                            has_valid_mark = False
                            for line in mf:
                                line = line.strip()
                                if not line: continue
                                parts = line.split(',')
                                if len(parts) >= 2:
                                    try:
                                        m = float(parts[1].strip())
                                        if m > max_marks:
                                            max_marks = m
                                        has_valid_mark = True
                                    except ValueError:
                                        pass
                            if has_valid_mark:
                                row[q] = max_marks
                                total_sum += max_marks
                            else:
                                row[q] = 0.0
                    except Exception:
                        row[q] = 0.0
                else:
                    row[q] = 0.0 if is_registered else "Absent"
            
            if not has_submitted_any:
                if is_registered:
                    # Registered but 0 work
                    for q in questions:
                        row[q] = 0.0
                    row["Total"] = 0.0
                else:
                    # Completely Absent
                    for q in questions:
                        row[q] = "Absent"
                    row["Total"] = "Absent"
            else:
                row["Total"] = round(total_sum, 2)
            
            grades_data.append(row)

        fieldnames = ["roll"] + questions + ["Total"]
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(grades_data)
        
        # 3. Add refined report to ZIP
        zf.writestr(f"grades_{lab_name}.csv", csv_buffer.getvalue())

        # 4. Add all submission files
        if os.path.exists(submissions_dir):
            for root, dirs, files in os.walk(submissions_dir):
                for file in files:
                    if file == ".DS_Store" or file == "submission.out" or file.startswith("actual_output_"):
                        continue
                    file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_path, submissions_dir)
                    # Layout is already ROLL/QNO/... — write as-is
                    zf.write(file_path, rel_path)
                    
    memory_file.seek(0)
    return Response(
        content=memory_file.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'}
    )

@app.get("/violations", response_class=HTMLResponse)
async def violations_page():
    try:
        return FileResponse("templates/violations.html", media_type="text/html")
    except FileNotFoundError:
        return HTMLResponse(content="<h1>404 — violations.html not found</h1>", status_code=404)



# --- Starter kit download ---
# validates roll + IP, registers the student, builds a personalized zip

@app.get("/starter/{roll_no}")
async def starter_kit(request: Request, roll_no: str):
    """Download a personalized starter kit .zip for this student.
    
    Integrity-protected: will not serve if project metadata is tampered.
    
    The first download from a machine also registers that machine to this roll number.
    Subsequent downloads from the same IP are fine. Downloads from a different IP
    are blocked (one PC per student enforcement).
    """
    effective_ip = get_client_ip(request)
    raw_roll = roll_no.upper()
    capitalized_roll_no = raw_roll.split('.')[0]  # strip any file extension from the URL
    
    # basic input sanitization — prevent path traversal attacks
    if not capitalized_roll_no.replace('_','').isalnum():
        return JSONResponse({"response": "Invalid roll number format."}, status_code=400)

    # must be in students.txt (or wildcard '*' means accept everyone)
    if capitalized_roll_no not in get_student_list() and "*" not in get_student_list():
        return JSONResponse({
            "response": "You are not registered for this course."
        }, status_code=status.HTTP_403_FORBIDDEN)

    # enforce whitelist (skip for admins)
    is_admin = getattr(request.state, "is_admin", False)
    if not is_admin and not is_authorized_system(effective_ip, allowed_systems):
        return JSONResponse({
            "response" : "YOUR SYSTEM IS NOT AUTHORIZED. Contact the instructor."
        })

    # Enforce one-PC-per-student: check if this IP is already bound to a different roll
    async with file_lock:
        registered_data = ip_roll_map.get(capitalized_roll_no)
        registered_ip = registered_data["ip"] if registered_data else None

        for existing_roll, existing_data in ip_roll_map.items():
            if existing_data["ip"] == effective_ip and existing_roll != capitalized_roll_no:
                return JSONResponse({
                    "response": "This system is already registered to another student. One PC can only be linked to one roll number."
                }, status_code=status.HTTP_403_FORBIDDEN)

        # If student is already registered on a DIFFERENT machine — block (they need the admin to unlink first)
        if registered_ip and registered_ip != effective_ip:
            return JSONResponse({
                "response": "This roll number is already registered on a different system. Contact the instructor to transfer your registration."
            }, status_code=status.HTTP_403_FORBIDDEN)

    # All checks passed — write/update the registration
    async with file_lock:
        ip_roll_map[capitalized_roll_no] = {
            "ip": effective_ip,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        if not os.path.exists(os.path.dirname(REGISTRATIONS_FILE)):
            os.makedirs(os.path.dirname(REGISTRATIONS_FILE), exist_ok=True)
        
        # Rewrite the entire CSV (it's small — one row per student)
        with open(REGISTRATIONS_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["roll_no", "ip_address", "timestamp"])
            for r_no, data in ip_roll_map.items():
                writer.writerow([r_no, data["ip"], data["timestamp"]])
    
    # ---- Build the personalized zip ----
    # The template zip lives in .active_lab/statics/. We clone it, rename the
    # folder to Lab_ROLLNO, and inject ROLL_NO + SERVER_URL into config.sh.
    statics_dir = ".active_lab/statics"
    try:
        # grab only the raw template zip (ignore previously built student zips with underscores)
        zip_files = [f for f in os.listdir(statics_dir) if f.endswith('.zip') and f.startswith('.') and '_' not in f]
        if not zip_files:
            return JSONResponse({"response": "Starter kit template zip not found on server. Ask instructor to build it."})

        zip_filename = zip_files[0]
        path_to_file = os.path.join(statics_dir, zip_filename)

        import zipfile, io

        # detect lab name from .active_lab symlink (same approach as download_submissions)
        lab_label = "Lab"
        try:
            real_path = os.path.realpath(os.path.join(".active_lab", "submissions"))
            parts = real_path.split(os.sep)
            if len(parts) >= 2:
                lab_label = parts[-2]  # e.g. "Lab9", "MidSem"
        except Exception:
            pass
        folder_prefix = f"{lab_label}_{capitalized_roll_no}"  # e.g. "Lab9_CS25M046"

        # ── Probe the source zip to decide how to rename ──
        # Strategy A: a folder containing 'XXX' → replace with folder_prefix
        # Strategy B: a single top-level wrapper folder → rename it to folder_prefix
        # Strategy C: flat / multiple roots → prepend folder_prefix
        template_folder_name = None   # set if Strategy A
        top_level_folder = None       # set if Strategy B
        rename_strategy = "C"         # default: wrap everything

        with zipfile.ZipFile(path_to_file, 'r') as probe_zip:
            top_level_names = set()
            for item in probe_zip.infolist():
                parts = [p for p in item.filename.split('/') if p]
                if not parts:
                    continue
                top_level_names.add(parts[0])
                # check for XXX marker
                for p in parts:
                    if 'XXX' in p.upper() and len(p) >= 4:
                        template_folder_name = p
                        break
                if template_folder_name:
                    break

            if len(top_level_names) == 1:
                top_level_folder = list(top_level_names)[0]
                rename_strategy = "B"
            elif template_folder_name:
                rename_strategy = "A"

        def rename_path(original_name: str) -> str:
            """Rename a zip entry path based on the detected strategy."""
            new_path = original_name
            if rename_strategy == "A" and template_folder_name:
                new_path = original_name.replace(template_folder_name, folder_prefix)
            elif rename_strategy == "B" and top_level_folder:
                if original_name == top_level_folder:
                    new_path = folder_prefix
                elif original_name.startswith(top_level_folder + "/"):
                    new_path = folder_prefix + original_name[len(top_level_folder):]
                else:
                    new_path = f"{folder_prefix}/{original_name}"
            else:
                new_path = f"{folder_prefix}/{original_name}"
            
            # Universal Rename: If we have a nested template folder (e.g. CS2XBXXX), 
            # rename it to JUST the student's roll number within the new structure.
            if template_folder_name and template_folder_name in new_path:
                # But only if Strategy A didn't already use it for the root
                if not (rename_strategy == "A" and original_name.split('/')[0] == template_folder_name):
                    new_path = new_path.replace(template_folder_name, capitalized_roll_no)
                    
            return new_path

        buf = io.BytesIO()
        with zipfile.ZipFile(path_to_file, 'r') as src_zip, \
             zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as dst_zip:
            config_found = False
            for item in src_zip.infolist():
                # skip nested zips
                if item.filename.endswith('.zip'):
                    continue
                
                # skip actual_output folder or other students' folders
                parts = [p for p in item.filename.split('/') if p]
                skip_item = False
                for p in parts:
                    if p.lower() == "actual_output":
                        skip_item = True
                        break
                    # If it looks like a roll number pattern
                    if re.match(r'^[A-Z]{2}[0-9]{2}[A-Z][0-9]{3}$', p):
                        # ONLY skip if it's NOT the target roll AND NOT the template folder
                        if p != capitalized_roll_no and p != template_folder_name:
                            skip_item = True
                            break
                if skip_item:
                    continue

                data = src_zip.read(item.filename)

                new_name = rename_path(item.filename)

                # inject ROLL_NO and SERVER_URL
                if item.filename.endswith("config.sh"):
                    config_found = True
                    text = data.decode("utf-8", errors="replace")
                    # regex-replace ROLL_NO value
                    text = re.sub(r'ROLL_NO="[^"]*"', f'ROLL_NO="{capitalized_roll_no}"', text)
                    if hasattr(request.state, "client_ip") and SERVER_IP != "127.0.0.1":
                         # use detected server IP
                         text = re.sub(r'SERVER_URL="[^"]*"', f'SERVER_URL="http://{SERVER_IP}:8000"', text)
                    else:
                         # fallback to Host header
                         host_url = request.headers.get("host", f"{SERVER_IP}:8000")
                         text = re.sub(r'SERVER_URL="[^"]*"', f'SERVER_URL="http://{host_url}"', text)
                         
                    data = text.encode("utf-8")

                info = item
                info.filename = new_name
                dst_zip.writestr(info, data)
                
            # make sure config.sh ends up inside the roll folder
            if not config_found:
                if hasattr(request.state, "client_ip") and SERVER_IP != "127.0.0.1":
                    server_url_str = f"http://{SERVER_IP}:8000"
                else:
                    host_url = request.headers.get("host", f"{SERVER_IP}:8000")
                    server_url_str = f"http://{host_url}"
                    
                config_content = f'ROLL_NO="{capitalized_roll_no}"\nSERVER_URL="{server_url_str}"\n'
                dst_zip.writestr(f"{folder_prefix}/config.sh", config_content.encode('utf-8'))

        buf.seek(0)
        personalized_name = f"{folder_prefix}.zip"

        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{personalized_name}"'}
        )
    except Exception as e:
        return JSONResponse({"response": f"Server error loading starter kit: {str(e)}"})


@app.get("/api/student-submissions/{roll_no}")
async def get_student_submissions(roll_no: str):
    _sub_dir = os.path.join(".active_lab", "submissions")
    if not os.path.isdir(_sub_dir):
        return JSONResponse(content=[])
    
    roll_no = roll_no.upper().strip()
    questions = []
    
    # Layout: submissions/ROLL/QNO — check this student's subdirs directly
    roll_dir = os.path.join(_sub_dir, roll_no)
    if os.path.isdir(roll_dir):
        for d in os.listdir(roll_dir):
            if os.path.isdir(os.path.join(roll_dir, d)):
                questions.append(d)
                
    questions.sort()
    return JSONResponse(content=questions)

@app.get("/api/questions")
async def get_questions():
    """Get question list from testcases (source of truth) + student submissions."""
    questions = set()
    # Primary source: testcases directory
    tc_dir = os.path.join(".active_lab", "testcases")
    if os.path.isdir(tc_dir):
        questions.update(d for d in os.listdir(tc_dir) if os.path.isdir(os.path.join(tc_dir, d)))
    # Fallback: scan student submission subdirs
    _sub_dir = os.path.join(".active_lab", "submissions")
    if os.path.isdir(_sub_dir):
        for roll in os.listdir(_sub_dir):
            roll_path = os.path.join(_sub_dir, roll)
            if os.path.isdir(roll_path):
                questions.update(d for d in os.listdir(roll_path) if os.path.isdir(os.path.join(roll_path, d)))
    return JSONResponse(content=sorted(questions))

@app.get("/leaderboard", response_class=HTMLResponse)
async def serve_leaderboard_index(request: Request):
    try:
        return FileResponse("templates/leaderboard_index.html")
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "leaderboard_index.html not found."})

@app.get("/leaderboard/{qno}", response_class=HTMLResponse)
async def serve_leaderboard_ui(request: Request, qno: str):
    try:
        return FileResponse("templates/leaderboard.html")
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "leaderboard.html not found."})
    
import time
_LEADERBOARD_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


@app.get("/api/leaderboard/{qno}")
async def get_leaderboard_data(qno: str):
    qno_upper = qno.upper()
    
    # sanitize
    if not qno_upper.replace('_','').isalnum():
        return JSONResponse(content=[])

    q_dir = os.path.join(".active_lab", "submissions")
    
    if not os.path.isdir(q_dir):
        return JSONResponse(content=[])

    # Cache bust: use the max mtime across all submissions/*/QNO dirs
    try:
        max_mtime = 0.0
        for roll in os.listdir(q_dir):
            qno_path = os.path.join(q_dir, roll, qno_upper)
            if os.path.isdir(qno_path):
                try:
                    mt = os.path.getmtime(qno_path)
                    if mt > max_mtime:
                        max_mtime = mt
                except OSError:
                    pass
        q_dir_mtime = max_mtime if max_mtime > 0 else time.time()
    except OSError:
        q_dir_mtime = time.time()
        
    # use cache if directory hasn't changed since last build
    if qno_upper in _LEADERBOARD_CACHE:
        cache_time, data = _LEADERBOARD_CACHE[qno_upper]
        if cache_time >= q_dir_mtime:
            return JSONResponse(content=data)

    leaderboard_data = []
    
    for roll_dir_name in os.listdir(q_dir):
        roll_path = os.path.join(q_dir, roll_dir_name)
        if not os.path.isdir(roll_path): continue
        student_q_path = os.path.join(roll_path, qno_upper)
        if os.path.isdir(student_q_path):
            marks_log_path = os.path.join(student_q_path, "marks.txt")
            if os.path.exists(marks_log_path):
                max_marks = -1
                try:
                    with open(marks_log_path, "r") as f:
                        for line in f:
                            try:
                                marks = float(line.strip().split(',')[1])
                                if marks > max_marks:
                                    max_marks = marks
                            except (IndexError, ValueError):
                                continue
                    if max_marks != -1:
                        leaderboard_data.append({"roll": roll_dir_name, "marks": max_marks})
                except Exception:
                    continue

    if not leaderboard_data:
        return JSONResponse(content=[])

    # sort descending by marks
    sorted_data = sorted(leaderboard_data, key=lambda x: x["marks"], reverse=True)

    # assign ranks (same marks = same rank)
    ranked_leaderboard = []
    last_mark = -1
    current_rank = 0
    for i, entry in enumerate(sorted_data):
        if entry["marks"] != last_mark:
            current_rank = i + 1
        
        ranked_leaderboard.append({
            "rank": current_rank,
            "roll": entry["roll"],
            "marks": entry["marks"]
        })
        last_mark = entry["marks"]
        
    _LEADERBOARD_CACHE[qno_upper] = (time.time(), ranked_leaderboard)
    return JSONResponse(content=ranked_leaderboard)


# --- Submission endpoint ---

@app.post("/submit/{qno}")
async def handleSubmit(
    qno: str,
    request: Request,
    roll: str = Form(...),
    file: UploadFile = File(...)
    ):
    """Handle student submissions. Async grading via Celery."""
    if not _verify_integrity():
        return JSONResponse(status_code=503, content={"response": "Server integrity check failed. Contact administrator."})

    effective_ip = get_client_ip(request)
    
    # Security: must be from the lab network
    is_admin = getattr(request.state, "is_admin", False)
    is_lab_network = any(effective_ip.startswith(subnet + ".") for subnet in LAB_SUBNETS)
    if not is_admin and not is_lab_network:
        reg_data = ip_roll_map.get(roll.upper())
        expected_ip = reg_data["ip"] if reg_data else "NA"
        await record_violation("Outside Network Submission", roll.upper(), expected_ip, effective_ip)
        return JSONResponse(status_code=403, content={"response": "Violation: Submission from outside the authorized lab network. This has been recorded."})

    qno_upper = qno.upper()
    roll_upper = roll.upper()

    # Rate limiting
    import time as _time
    now = _time.time()
    last_submit = _submission_cooldowns.get(roll_upper, 0)
    if now - last_submit < SUBMIT_COOLDOWN_SECONDS and not is_admin:
        remaining = int(SUBMIT_COOLDOWN_SECONDS - (now - last_submit)) + 1
        return JSONResponse(status_code=429, content={"response": f"Rate limited. Please wait {remaining}s before submitting again."})
    _submission_cooldowns[roll_upper] = now

    # Input sanitization
    if not qno_upper.replace('_','').isalnum() or not roll_upper.replace('_','').isalnum():
        return JSONResponse(status_code=400, content={"response": "Invalid question or roll number."})

    # Must be in the authorized student list
    if roll_upper not in get_student_list() and "*" not in get_student_list():
        return JSONResponse(status_code=403, content={"response": "You are not registered for this course."})

    # Must have downloaded the starter kit first (which registers their IP)
    registered_data = ip_roll_map.get(roll_upper)
    registered_ip = registered_data["ip"] if registered_data else None
    
    if not registered_ip:
        return JSONResponse(status_code=403, content={"response": f"Roll number '{roll_upper}' is not registered. Please download the starter kit first to register your system."})
    
    # IP must match
    if effective_ip != registered_ip:
        await record_violation("Submit IP Mismatch", roll_upper, registered_ip, effective_ip)
        return JSONResponse(status_code=403, content={"response": f"Violation: Submission from unregistered IP ({effective_ip})."})

    # All checks passed — read the file and queue it for grading
    file_content = await file.read()
    file_content_str = file_content.decode('utf-8', errors='ignore')

    # Queue task for Celery
    work = handle_submission.delay(qno_upper, roll_upper, file.filename, file_content_str)    
    return JSONResponse({"taskid": work.id})


# --- Utility endpoints ---

@app.get("/api/detect-roll")
async def detect_roll(request: Request):
    """Auto-detect the student's roll number from their IP.
    Used by the frontend to pre-fill roll number fields."""
    client_ip = get_client_ip(request)
    
    for roll_no, data in ip_roll_map.items():
        if data.get("ip") == client_ip:
            return JSONResponse({"roll_no": roll_no})
            
    return JSONResponse({"roll_no": None})

@app.get("/recover", response_class=HTMLResponse)
async def recover_page():
    """Code recovery page — students can recover their last submitted source file."""
    try:
        return FileResponse("templates/recover.html", media_type="text/html")
    except FileNotFoundError:
        return HTMLResponse(content="<h1>404 — recover.html not found</h1>", status_code=404)

@app.post("/api/request-recovery/{qno}/{roll}")
async def request_recovery(qno: str, roll: str, request: Request):
    """Student requests code recovery. Creates a pending request for admin approval."""
    roll_upper = roll.upper()
    qno_upper = qno.upper()

    if not roll_upper.replace('_','').isalnum() or not qno_upper.replace('_','').isalnum():
        raise HTTPException(status_code=400, detail="Invalid roll number or question number.")

    if roll_upper not in get_student_list() and "*" not in get_student_list():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not registered for this course.")

    std_dir = os.path.join(".active_lab", "submissions", roll_upper, qno_upper)
    if not os.path.isdir(std_dir):
        raise HTTPException(status_code=404, detail="No submissions found for this question.")

    global total_recovery_count
    total_recovery_count += 1
    
    if roll_upper not in recovery_requests:
        recovery_requests[roll_upper] = {}
    effective_ip = get_client_ip(request)
    recovery_requests[roll_upper][qno_upper] = {"status": "pending", "ip": effective_ip}
    save_recovery_requests()
    print(f"[RECOVERY] Request from {roll_upper} for {qno_upper} (IP: {effective_ip})")

    return JSONResponse({"status": "pending", "message": "Recovery request submitted. Waiting for admin approval."})

@app.get("/api/recovery-status/{qno}/{roll}")
async def get_recovery_status(qno: str, roll: str):
    """Student polls this to check if their recovery request was approved."""
    roll_upper = roll.upper()
    qno_upper = qno.upper()
    req_data = recovery_requests.get(roll_upper, {}).get(qno_upper)
    if not req_data:
        return JSONResponse({"status": "none"})
    return JSONResponse({"status": req_data["status"]})

@app.get("/api/recover/{qno}/{roll}")
async def recover_code(qno: str, roll: str, request: Request):
    """Return the student's most recent source file for a given question.
    
    Requires admin approval (or admin bypass) before serving the file.
    """
    effective_ip = get_client_ip(request)
    roll_upper = roll.upper()
    qno_upper = qno.upper()
    is_admin = getattr(request.state, "is_admin", False)

    if not roll_upper.replace('_','').isalnum() or not qno_upper.replace('_','').isalnum():
        raise HTTPException(status_code=400, detail="Invalid roll number or question number.")

    if roll_upper not in get_student_list() and "*" not in get_student_list():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not registered for this course.")

    # Admins can always recover; students need approval
    if not is_admin:
        req_data = recovery_requests.get(roll_upper, {}).get(qno_upper)
        if not req_data or req_data["status"] != "approved":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Recovery not approved yet. Please wait for admin approval.")

    # verify IP match (admins bypass)
    registered_data = ip_roll_map.get(roll_upper)
    registered_ip = registered_data["ip"] if registered_data else None
    if not registered_ip or effective_ip != registered_ip:
        if not is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP mismatch. Please download the starter kit on this computer first.")
    
    std_dir = os.path.join(".active_lab", "submissions", roll_upper, qno_upper)
    if not os.path.isdir(std_dir):
        raise HTTPException(status_code=404, detail="No submissions found for this question.")
        
    import glob
    search_pattern = os.path.join(std_dir, "**", "*.*")
    files = glob.glob(search_pattern, recursive=True)
    
    source_files = [f for f in files if os.path.isfile(f) and not f.endswith('.out') and not f.endswith('.txt') and 'sandbox' not in f and os.path.basename(f).startswith(f"{qno_upper}_")]
    
    if not source_files:
        source_files = [f for f in files if os.path.isfile(f) and (f.endswith('.cpp') or f.endswith('.py') or f.endswith('.c')) and 'sandbox' not in f]
    
    if not source_files:
        raise HTTPException(status_code=404, detail="No source code files found to recover.")
        
    latest_file = max(source_files, key=os.path.getctime)
    filename = os.path.basename(latest_file)

    # Consume the approval so it can't be reused
    if not is_admin and roll_upper in recovery_requests:
        recovery_requests[roll_upper].pop(qno_upper, None)
        if not recovery_requests[roll_upper]:
            recovery_requests.pop(roll_upper, None)
        save_recovery_requests()
    
    print(f"[RECOVERY] File served: {filename} for {roll_upper}")
    return FileResponse(latest_file, media_type='application/octet-stream', filename=filename)

# --- Admin recovery management endpoints ---

@app.get("/api/recovery-requests")
async def list_recovery_requests():
    """Admin-only: list all pending recovery requests with their IP info."""
    pending = []
    for roll, q_dict in recovery_requests.items():
        for qno, info in q_dict.items():
            if info.get("status") == "pending":
                pending.append({
                    "roll": roll,
                    "qno": qno,
                    "ip": info.get("ip")
                })
    return JSONResponse({
        "requests": pending, 
        "count": len(pending),
        "total_cumulative": total_recovery_count
    })

@app.post("/admin/approve-recovery")
async def approve_recovery(roll: str, qno: str, request: Request):
    """Admin approves a student's recovery request."""
    roll_upper = roll.upper()
    qno_upper = qno.upper()
    if roll_upper in recovery_requests and qno_upper in recovery_requests[roll_upper]:
        recovery_requests[roll_upper][qno_upper]["status"] = "approved"
        save_recovery_requests()
        print(f"[RECOVERY] Approved {roll_upper} for {qno_upper}")
        return JSONResponse({"status": "approved"})
    return JSONResponse({"status": "not_found", "message": "No pending request found."}, status_code=404)

@app.post("/admin/reject-recovery")
async def reject_recovery(roll: str, qno: str, request: Request):
    """Admin rejects a student's recovery request."""
    roll_upper = roll.upper()
    qno_upper = qno.upper()
    if roll_upper in recovery_requests and qno_upper in recovery_requests[roll_upper]:
        recovery_requests[roll_upper].pop(qno_upper, None)
        if not recovery_requests[roll_upper]:
            recovery_requests.pop(roll_upper, None)
        save_recovery_requests()
        print(f"[RECOVERY] Rejected {roll_upper} for {qno_upper}")
        return JSONResponse({"status": "rejected"})
    return JSONResponse({"status": "not_found", "message": "No pending request found."}, status_code=404)

@app.get("/admin/recovery-requests", response_class=HTMLResponse)
async def recovery_requests_page():
    """Admin page showing pending recovery requests."""
    try:
        return FileResponse("templates/recovery_requests.html", media_type="text/html")
    except FileNotFoundError:
        return HTMLResponse(content="<h1>404 — recovery_requests.html not found</h1>", status_code=404)

@app.get("/status", response_class=HTMLResponse)
async def submission_status_page():
    try:
        return FileResponse("templates/status.html", media_type="text/html")
    except FileNotFoundError:
        return HTMLResponse(content="<h1>404 — status.html not found</h1>", status_code=404)

@app.get("/api/status")
async def get_submission_status_api():
    base_submission_dir = os.path.join(".active_lab", "submissions")
    
    # find all possible question IDs from testcases (ground truth)
    # and submissions (in case some were manually added/changed)
    testcases_dir = os.path.join(".active_lab", "testcases")
    
    question_ids = set()
    if os.path.isdir(testcases_dir):
        question_ids.update([
            d for d in os.listdir(testcases_dir)
            if os.path.isdir(os.path.join(testcases_dir, d))
        ])
    # With ROLL/QNO layout, top-level dirs in submissions are student rolls
    # Initialize registered students from ip_roll_map + registrations.csv
    registered_students = set(ip_roll_map.keys())
    import csv
    registrations_file = os.path.join(".active_lab", "registrations.csv")
    if os.path.exists(registrations_file):
        try:
            with open(registrations_file, "r", newline="", encoding="utf-8") as rf:
                rdr = csv.DictReader(rf)
                for row in rdr:
                    if row.get("roll_no"):
                        registered_students.add(row["roll_no"].upper())
        except Exception:
            pass
    
    # With ROLL/QNO layout, top-level dirs in submissions are student rolls
    if os.path.isdir(base_submission_dir):
        # Collect question IDs from student subdirs
        for roll in os.listdir(base_submission_dir):
            roll_path = os.path.join(base_submission_dir, roll)
            if os.path.isdir(roll_path):
                registered_students.add(roll.upper())
                for qd in os.listdir(roll_path):
                    if os.path.isdir(os.path.join(roll_path, qd)):
                        question_ids.add(qd)
    
    question_dirs = sorted(list(question_ids))
    
    report = {}
    
    for roll_no in sorted(list(registered_students)):
        student_status = {}
        for qno in question_dirs:
            submission_path = os.path.join(base_submission_dir, roll_no, qno)
            student_status[qno] = os.path.isdir(submission_path)
        report[roll_no] = student_status

    return JSONResponse(content=report)


# --- Task status + static file mounts ---

@app.get("/task-status/{task_id}")
async def get_task_status(task_id: str):
    """Poll the status of a Celery grading task."""

    async_result = AsyncResult(task_id)
    
    result = async_result.result
    if isinstance(result, Exception):
        result = str(result)  # serialize exceptions so JSON doesn't choke
        
    return JSONResponse(
        {
            "task-id": task_id,
            "status": async_result.status,
            "result": result
        }
    )

# Static file mounts — these MUST come after all route definitions.
# If mounted before, they'd shadow our dynamic routes.

# Note: cppreference is served via the /cppref/{path} reverse proxy route above.
# No static mount needed — the server streams from en.cppreference.com on-demand.


# Mount downloadable resources (PDFs, lecture slides, etc.)
offline_files_dir = ".active_lab/offline_files"
if os.path.isdir(offline_files_dir):
    app.mount("/offline_files", StaticFiles(directory=offline_files_dir), name="offline_files")

# Mount starter kit static files (the dynamic /starter/{roll_no} route takes priority
# over this mount for personalized downloads)
app.mount("/starter", StaticFiles(directory=".active_lab/statics"), name="statics")

# Mount self-hosted fonts and other static assets (CSS, images, etc.)
if os.path.isdir("assets"):
    app.mount("/assets", StaticFiles(directory="assets"), name="assets")

# Entry point for running directly with `python main.py` (normally started via start.sh + uvicorn)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


