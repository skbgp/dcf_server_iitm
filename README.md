# DCF Exam Server

This is an offline exam server originally built for the Department of Computer Science and Engineering at IIT Madras. We use it to host and grade programming lab exams in a completely isolated local network, without needing cloud resources or internet access.

The main problem this solves is running student code securely and grading it automatically, while making sure students and the server stay completely offline. 

## Features

- **Completely Offline Grading:** Designed for isolated LAN environments without cloud dependencies.
- **Asynchronous Evaluation:** Powered by FastAPI, Celery, and Redis to handle concurrent submissions smoothly.
- **Secure Sandboxing:** Uses Linux namespaces via Bubblewrap (`bwrap`) to execute code with strict CPU, memory, and network isolation.
- **IP Address Binding:** Automatically locks student accounts to their initial machine's IP, preventing exam sharing.
- **Live Admin Dashboard:** Real-time metrics, access logs, access control, and IP whitelisting for designated lab rooms.
- **Integrated Plagiarism Detection:** Built-in proxy for Stanford MOSS to analyze similarity across all student submissions instantly.
- **Simple Student CLI:** Students receive a fully packaged `.zip` environment with custom `./check.sh` and `./submit.sh` tools.
- **Live Leaderboard:** Per-question leaderboards that update as submissions are graded.
- **Code Recovery:** Built-in request system for students whose machines crash mid-exam to cleanly rebind to a new IP.
- **Practice Kit Generator:** After the exam, merge public and private test cases into a single offline-friendly practice zip.
- **Grades Export:** One-click download of all submissions and a consolidated grades CSV from the admin panel.
- **Emergency Recovery:** Built-in request system for students whose machines crash mid-exam to cleanly rebind to a new IP.

## How it works

The core of the system is built with FastAPI for the web interface and Celery + Redis for a background grading queue, which keeps the main web server responsive even when evaluating lots of complex C++ code.

Instead of heavy Docker containers, we use **Bubblewrap** (`bwrap`) to run student submissions in Linux namespaces. This is fast and lets us completely isolate the process — the code has no network access, runs in a read-only root filesystem, and has strict memory and time limits. This stops accidental infinite loops or malicious behavior.

The actual grading flow looks something like this:

1. Student runs `./submit.sh` → sends `.cpp` file to server via `curl`
2. FastAPI receives it at `/submit/Q1` → queues a Celery task
3. Celery worker picks it up → compiles with `g++` inside bwrap sandbox
4. Runs the binary against every test case (parallel, up to 8 at a time)
5. Compares output token-by-token → calculates marks proportionally
6. Updates `grades.csv` with the student's **best** score (grades never go down)
7. Returns results to student's terminal

### Security features

Because it's meant for exams, there are a few built-in security mechanisms:
- **IP Binding:** The first time a student downloads their exam kit using their roll number, the server permanently binds that roll number to their machine's LAN IP address for the duration of the exam.
- **Admin Dashboard:** Admins get a real-time view of everyone connected. You can manually whitelist entire IP ranges (like `10.21.224.*`) for a particular lab room.
- **Violation Logging:** If a student tries to download a kit or submit code from outside the allowed IP range (e.g. from an unauthorized mobile hotspot), they are blocked and it shows up live on the admin dashboard.
- **MOSS Plagiarism Checking:** The admin dashboard integrates directly with Stanford's MOSS. You can trigger a plagiarism check across all submissions with one click. The server uses `mosspy` to upload the code, and then displays the results directly in the dashboard via a built-in proxy.
- **Rate Limiting:** There's a 5 second cooldown between submissions to prevent queue spamming.

### What the students see

We wanted the workflow to be as simple as possible for the students taking the exam:

1. They type the server IP into their browser, enter their roll number, and download a `.zip` starter kit.
2. They unzip it, read the PDF, and write their code in the included `Q1.cpp`, `Q2.cpp` templates.
3. To test locally on their public test cases, they can just run `./check.sh`.
4. To finally submit for grading, they run `./submit.sh`. The terminal talks directly to the server's API, gets graded in the background sandbox, and prints their score.

## Project structure

```
dcf_server_iitm/
├── main.py                 # FastAPI web server — all routes, middleware, security
├── task.py                 # Celery worker — compilation, sandboxed execution, grading
├── start.sh                # Main launcher — installs deps, starts everything
├── create_lab.sh           # Interactive wizard to set up a new lab
├── generate_practice.sh    # Generates offline practice kits after exam ends
├── reset.sh                # Factory-resets a lab (wipes submissions, grades, etc.)
├── requirements.txt        # Python dependencies
├── default_scripts/        # Template scripts shipped in every student starter kit
│   ├── submit.sh
│   ├── check.sh
│   ├── config.sh
│   └── README.md
├── templates/              # HTML files for the web interface
│   ├── index.html          # Student homepage
│   ├── admin.html          # Admin dashboard
│   ├── system_access.html  # IP whitelist management
│   ├── violations.html     # Security violations viewer
│   ├── leaderboard.html    # Per-question leaderboard
│   ├── recover.html        # Code recovery page
│   ├── status.html         # Submission status grid
│   ├── docs_hub.html       # Offline documentation hub
│   └── error.html          # Custom error pages
├── assets/                 # Fonts
├── cppreference/           # (optional) Offline C++ docs
└── courses/                # (created at runtime, gitignored)
    └── <COURSE>/
        ├── students.txt
        ├── offline_files/
        └── <LAB>/
            ├── course.conf
            ├── testcases/Q1/{input,output}/
            ├── statics/<LAB>/  (starter kit template)
            ├── submissions/
            │   └── <ROLL>/          (student-first layout)
            │       └── <QNO>/
            │           ├── marks.txt         (append-only log: timestamp, marks)
            │           └── <TIMESTAMP>/      (one folder per submission attempt)
            │               └── <ROLL>.cpp
            ├── registrations.csv
            ├── violations.csv
            └── grades.csv
```

## Getting started

### Requirements

You'll need a Linux environment (Ubuntu/Debian recommended) to use Bubblewrap properly. You also need `python3`, `pip`, `g++`, `redis-server`, and `bubblewrap` installed on the host. 

macOS works for development but lacks full native isolation — `bwrap` doesn't exist on macOS, so student code runs without a sandbox. Never use macOS as an actual exam server with untrusted code. Python packages from `requirements.txt` are automatically installed by the start script.

### Clone and go

```bash
git clone https://github.com/skbgp/dcf_server_iitm.git
cd dcf_server_iitm
```

From here you need to:
1. Create a lab (set up questions, marks, timeouts)
2. Add your test cases and question papers
3. Add the student roll numbers
4. Start the server

Each of these steps is explained below.

---

## Setting up a lab — `create_lab.sh`

This is an interactive wizard that walks you through creating a new lab environment. It sets up the full directory structure, empty data files, starter kit templates, and course configuration.

### Running it

```bash
chmod +x create_lab.sh   # only needed the first time
./create_lab.sh
```

It'll ask you a few things:

1. **Pick a course** — either select an existing one from the list, or type `N` to create a new one (e.g., `CS2810`).
2. **Lab name** — something like `Lab7` or `MidSem`.
3. **Number of questions** — e.g., `3`.
4. **Marks and timeout for each question** — it'll loop through each question and ask how many marks it's worth and how many seconds the student's code gets to run.

When it's done, you'll see the created directory structure and a list of what you need to do next.

### What you need to add manually after running it

The script creates the skeleton. You still need to fill in the actual exam content:

**Student list** — Put all authorized roll numbers in `courses/<COURSE>/students.txt`, one per line:
```
CS24B001
CS24B002
CS24B003
```

**Question paper** — Drop your exam PDF into `courses/<COURSE>/<LAB>/statics/<LAB>/`. Students get this in their zip.

**Private test cases** (the ones used for actual grading on the server) — Go into `courses/<COURSE>/<LAB>/testcases/Q1/input/` and `courses/<COURSE>/<LAB>/testcases/Q1/output/`. Name them `input01.txt`, `input02.txt`, etc. and `output01.txt`, `output02.txt`, etc. The numbering has to match.

**Public test cases** (optional, shipped to students) —  Put these in `courses/<COURSE>/<LAB>/statics/<LAB>/testcases/Q1/input/` and `.../output/`. Same naming convention. Students use these with `./check.sh` to test locally before submitting.

**Offline files** (optional) — Any extra PDFs, reference docs, datasets can go in `courses/<COURSE>/offline_files/`. Students can download these from the web interface.

---

## Starting the server — `start.sh`

This is the main launcher. It handles literally everything — dependency installation, virtual environments, service cleanup, IP detection, starter kit zipping, and starting Redis + Celery + FastAPI with a crash watchdog.

### Running it

```bash
# Interactive — gives you menus to pick course and lab
./start.sh

# Or skip the menus
./start.sh CS2810 Lab7
```

On **Linux** (Debian/Ubuntu), it'll automatically install anything missing: `redis-server`, `bubblewrap`, `pip3`, `lsof`, `netcat`, `python3-venv`, and all the Python packages from `requirements.txt`.

On **macOS**, it creates a virtualenv at `~/serverenv`, activates it, and installs the Python packages. You need to have Redis installed yourself (`brew install redis`).

### What happens when you run it

1. Kills any leftover FastAPI/Celery/Redis processes from a previous run
2. Shows you course and lab selection menus (if you didn't pass arguments)
3. Installs missing dependencies
4. Creates `.active_lab/` — a directory of symlinks pointing to the selected lab's files
5. Detects your LAN IP and writes it into the starter kit's `config.sh`
6. Zips up the starter kit template
7. Starts Redis (port 6379), Celery workers, and FastAPI (port 8000)
8. Prints the server URL and enters a watchdog loop

You'll see something like:

```
=======================================
Server is live! (CS2810 / Lab7)
Main URL: http://10.21.224.50:8000
=======================================
Press [CTRL+C] to shut down all services.
```

Press `Ctrl+C` to gracefully shut everything down. If any service crashes, the watchdog detects it and shuts down the rest automatically.

---

## Resetting a lab — `reset.sh`

This wipes all student data for a specific lab — submissions, registrations, grades, violations. It's meant for when you're running the same lab with a new batch of students.

**This is destructive. There's no undo.**

```bash
# Interactive
./reset.sh

# Or pass course and lab directly
./reset.sh CS2810 Lab7
```

It'll show you exactly what it's about to delete and ask for confirmation. If you say yes:
- Stops the running server first (to avoid file lock conflicts)
- Deletes everything inside `submissions/`
- Resets `registrations.csv`, `violations.csv`, and `grades.csv` back to just headers
- Clears the `.active_lab/` symlink cache

After resetting, just run `./start.sh` again to start fresh.

---

## Generating practice kits — `generate_practice.sh`

After the exam is completely over and you want to give students access to the full test suite for practice, run this:

```bash
./generate_practice.sh
```

It'll ask you to pick a course and lab, then:
- Copies the starter `.cpp` files and question PDF
- Merges public and private test cases together (public ones get a `_pub` suffix to distinguish them)
- Reads per-question timeouts from `course.conf` and bakes them into the scripts, so students practice with the same time limits as the real exam
- Generates `practice.sh` (Mac/Linux) and `practice.bat` (Windows) — standalone scripts that compile and test code locally against all test cases
- Creates a `README.md` with instructions for students
- Zips everything into `practice_exports/<COURSE>_<LAB>_Practice.zip`

Share that zip with students. They unzip it, write their code, and run `./practice.sh` to test — no server needed.

---

## Using the admin panel

The admin dashboard is restricted. You have to access the server from the host machine itself (or an explicitly allowed IP) to see it. When you open the server URL from the server machine, it automatically redirects to `/admin`.

### Opening the server to students

Before the exam starts, go to **System Access** and enter the IP subsets for the lab rooms (e.g., `10.21.*.*` or `224.*`). You can also just grant `*` to let all lab network machines in. Revoking `*` effectively closes the server.

### If a student needs to switch machines

Their PC crashed, they moved seats — whatever. Their new IP won't match their bound IP. They go to `http://<SERVER_IP>:8000/recover`, submit a recovery request, and you click "Approve" on the admin dashboard. They can then re-download their starter kit and their latest submitted code.

### Downloading all submissions

Click "Download All Submissions" in the admin panel. You get a ZIP with every student's code organized by roll number and question (matching the on-disk `submissions/ROLL/QNO/` layout), plus a `grades.csv` summarizing everyone's best marks per question.

### Deleting a student's submission

If a student needs a redo for whatever reason, you can delete their submission from the dashboard. Their grade resets and they can submit again.

---

## MOSS Plagiarism Detection

[MOSS](https://theory.stanford.edu/~aiken/moss/) (Measure Of Software Similarity) is a free plagiarism detection service run by Stanford. We've integrated it directly into the admin dashboard.

### How it works here

When you click "Run MOSS" from the admin panel, the server:

1. Scans all student submissions for the question you selected
2. For each student, picks their **best-scoring** submission (based on `marks.txt`)
3. Uses the `mosspy` Python library to connect to `moss.stanford.edu` on **TCP port 7690** and uploads all the `.cpp` files
4. MOSS analyzes the code using a document fingerprinting algorithm and returns a URL to the results page
5. The server saves the URL to `courses/<COURSE>/moss_history.csv` and opens the report in a new tab

The report is served through a **built-in reverse proxy** with a dark-themed UI, so you don't have to worry about iframe restrictions or leaving the dashboard.

For "run all questions", it loops through each question directory (with a 3-second gap between requests to avoid rate limiting), and all generated report URLs go into MOSS History.

### Getting a MOSS ID

MOSS requires a free user ID from Stanford. You only need to do this once:

1. Send an email to `moss@moss.stanford.edu` with this body:
   ```
   registeruser
   mail your-email@example.com
   ```

2. You'll get a reply with a Perl script. Look for this line:
   ```perl
   $userid = 123456789;
   ```
   That number is your MOSS ID. Save it. You'll enter it in the admin panel each time you run a check.

### Port 7690

This is the part that trips everyone up. MOSS does **not** use HTTP. The `mosspy` client opens a raw TCP socket to `moss.stanford.edu` on **port 7690**. If your network blocks this port (which most campus firewalls do), MOSS will silently fail with a timeout.

**Quick test — run this on the server:**
```bash
nc -zv moss.stanford.edu 7690
```
If it says "succeeded", you're good. If it times out, port 7690 is blocked.

**Python test (same thing mosspy does internally):**
```bash
python3 -c "import socket; s=socket.socket(); s.settimeout(5); s.connect(('moss.stanford.edu', 7690)); print('Port 7690 is OPEN'); s.close()"
```

### Opening port 7690

**On the server itself (if it has a firewall):**

```bash
# Ubuntu/Debian (ufw)
sudo ufw allow out 7690/tcp
sudo ufw reload

# iptables
sudo iptables -A OUTPUT -p tcp --dport 7690 -j ACCEPT

# CentOS/RHEL (firewalld)
sudo firewall-cmd --permanent --add-port=7690/tcp
sudo firewall-cmd --reload
```

**On macOS** — the built-in macOS firewall doesn't block outbound connections, so this usually isn't an issue. If you're running Little Snitch or Lulu, allow outbound TCP to `moss.stanford.edu` on port 7690.

**Campus / institutional firewall** — this is the most common problem. Your campus IT restricts outbound traffic to ports 80 and 443 only. You need to email them something like:

> We need outbound TCP access from our server (IP: `<your server IP>`) to `moss.stanford.edu` (IP: `171.64.78.49`) on port `7690`. This is for Stanford's MOSS plagiarism detection service that we use for academic integrity checks.

**If IT won't budge**, here are some workarounds:

1. **Google Colab Fallback (Built-in + Recommended)** — If the MOSS check fails due to a blocked port, a warning and a **"Download Colab Notebook"** button will appear. Clicking it generates a self-contained Jupyter notebook (`.ipynb`) with all the student code files embedded inside. 
   - Open [colab.google.com](https://colab.google.com/) and upload the notebook.
   - Click "Run All" in Colab. It will automatically decode the files and run `mosspy` on Google's unrestricted servers.
   - Copy the generated MOSS report URL.
   - Paste it back into the admin panel using the **"Import MOSS URL"** input to save it to your MOSS History.
   - Note: This fallback naturally supports selecting "All" or leaving the question blank to process all questions.
2. **Mobile hotspot** — Connect the server to a phone hotspot temporarily, run MOSS, switch back to LAN. Phone networks don't block port 7690.
3. **Run from another machine** — Copy the submissions folder to a laptop on unrestricted WiFi, install mosspy (`pip install mosspy`), run MOSS manually, paste the report URL.
4. **SSH tunnel** — If you have access to any server outside the firewall:
   ```bash
   ssh -L 7690:moss.stanford.edu:7690 user@your-external-server
   # Then point moss.stanford.edu to 127.0.0.1 in /etc/hosts
   ```

### MOSS History

Every report URL gets logged to `courses/<COURSE>/moss_history.csv`. You can view and manage all past reports at `/admin/moss-history-page`.

Keep in mind that MOSS report URLs expire after about 14 days on Stanford's end, so download or screenshot anything important while it's still live.

---

## Network and firewall settings

Two ports matter:

| Port | Direction | Purpose |
|---|---|---|
| **8000** | Inbound | Students access the server on this port. Make sure it's open on your LAN firewall. |
| **7690** | Outbound | MOSS plagiarism checking connects to Stanford on this port. Only needed from the server machine. |

If port 8000 is blocked, students can't reach anything. If port 7690 is blocked, everything works except MOSS.

---

## Troubleshooting

**"Cannot find the project root"** — The scripts look for `start.sh` in the same directory. Make sure you're running them from the project root, not from somewhere else.

**Port 8000 already in use** — `start.sh` tries to kill whatever's on port 8000 automatically. If it fails: `lsof -i :8000` to find the PID, then `kill -9 <PID>`.

**Redis won't start** — Check if it's already running: `redis-cli ping` (should return `PONG`). If not: `redis-server &`.

**"bubblewrap (bwrap) not found"** — Expected on macOS (runs in dev mode without sandbox). On Linux: `sudo apt-get install bubblewrap`.

**"YOUR SYSTEM IS NOT AUTHORIZED"** — The admin hasn't opened access yet. Go to System Access in the admin panel and grant access.

**"This roll number is already registered on a different system"** — That student's roll is bound to their old machine. Delete their registration from the admin dashboard, then they can re-download.

**Submissions are slow / unexpected TLE** — On macOS, the first compile after reboot triggers Gatekeeper scanning which adds a couple seconds. Also check if `course.conf` timeouts are too low.

**Permission denied on scripts** — `chmod +x start.sh create_lab.sh reset.sh generate_practice.sh`

**Server logs:**
```bash
tail -f logs/fastapi.log    # Web server
tail -f logs/celery.log     # Grading workers
tail -f logs/redis.log      # Message broker
```

---

Built by Shubham (CS25M046) for IIT Madras.
