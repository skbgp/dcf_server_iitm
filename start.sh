#!/bin/bash
# start.sh - main launcher, handles deps, symlinks, and starts all services
# usage: ./start.sh              (interactive)
#        ./start.sh CS2810 Lab5  (direct)
#        ./start.sh -v           (verbose output for debugging)

# ---- VERBOSE FLAG ----
VERBOSE=${VERBOSE:-false}
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "-v" ] || [ "$arg" = "--verbose" ]; then
        VERBOSE=true
    else
        ARGS+=("$arg")
    fi
done
set -- "${ARGS[@]}"

PORT=8000           # the HTTP port for the FastAPI server
COURSE=${1}         # first argument: course name (e.g. CS2810)
LAB=${2}            # second argument: lab name (e.g. Lab5)

# Terminal colors & styles
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'        # reset color

# Print helpers — verbose-only vs always-visible
log()           { [ "$VERBOSE" = true ] && echo -e "  ${DIM}$1${NC}"; }
log_ok()        { [ "$VERBOSE" = true ] && echo -e "  ${GREEN}[OK]${NC} $1"; }
log_skip()      { [ "$VERBOSE" = true ] && echo -e "  ${DIM}[--] $1${NC}"; }
print_warn()    { echo -e "  ${YELLOW}[!!]${NC} $1"; }
print_fail()    { echo -e "  ${RED}[FAIL]${NC} $1"; }

# ---- BANNER ----
show_banner() {
    echo ""
    echo -e "${BOLD}=======================================${NC}"
    echo -e "${BOLD}    DCF Exam Server Launcher           ${NC}"
    echo -e "${BOLD}=======================================${NC}"
    echo ""
}

# ---- SERVICE CLEANUP ----
stop_existing_services() {
    log "Cleaning up previous sessions..."
    
    pkill -f "uvicorn main:app" 2>/dev/null && log_ok "Stopped previous FastAPI server" || true
    pkill -f "celery -A task.capp worker" 2>/dev/null && log_ok "Stopped previous Celery workers" || true
    
    if lsof -i:6379 > /dev/null 2>&1; then
        if command -v redis-cli &> /dev/null && redis-cli ping &> /dev/null; then
            log_skip "Redis already running and healthy — keeping it"
        else
            log "Redis on 6379 is unresponsive — restarting"
            pkill -f "redis-server" 2>/dev/null
        fi
    fi
    
    pkill -f "python3 main.py" 2>/dev/null
    pkill -f "python3 task.py" 2>/dev/null
    
    PORT_PID=$(lsof -t -i:$PORT 2>/dev/null)
    if [ -n "$PORT_PID" ]; then
        kill -9 $PORT_PID 2>/dev/null
        sleep 1
    fi
}

# List valid labs inside a course directory.
list_labs() {
    local cdir=$1
    if [ -d "$cdir" ]; then
        cd "$cdir" && ls -d */ 2>/dev/null | sed 's|/||g' | grep -vE "^(offline_files|cppreference|testcases|submissions|statics)$"
        cd - > /dev/null
    fi
}

# ---- MAIN FLOW ----
show_banner
stop_existing_services

# ---- INTERACTIVE SELECTION ----
if [ -z "$COURSE" ] || [ -z "$LAB" ]; then

    # Course selection
    echo -e "${BOLD}Select a course:${NC}"
    COURSE_ARRAY=()
    i=1
    if [ -d "./courses" ]; then
        for c in ./courses/*/; do
            if [ -d "$c" ]; then
                cname=$(basename "$c")
                COURSE_ARRAY+=("$cname")
                lab_count=$(find "$c" -maxdepth 1 -mindepth 1 -type d ! -name "offline_files" ! -name "cppreference" 2>/dev/null | wc -l | tr -d ' ')
                echo -e "  ${CYAN}${BOLD}$i)${NC} $cname ${DIM}($lab_count labs)${NC}"
                ((i++))
            fi
        done
    fi

    if [ ${#COURSE_ARRAY[@]} -eq 0 ]; then
        echo ""
        print_fail "No courses found in ./courses/"
        echo -e "  ${DIM}Run ${BOLD}./create_lab.sh${NC}${DIM} first to set up a lab.${NC}"
        exit 1
    fi

    echo ""
    read -p "$(echo -e "${YELLOW}Enter choice (1-$((i-1))): ${NC}")" COURSE_CHOICE
    if [[ "$COURSE_CHOICE" =~ ^[0-9]+$ ]] && [ "$COURSE_CHOICE" -ge 1 ] && [ "$COURSE_CHOICE" -lt "$i" ]; then
        COURSE=${COURSE_ARRAY[$((COURSE_CHOICE-1))]}
    else
        print_fail "Invalid choice."; exit 1
    fi

    # Lab selection
    echo -e "\n${BOLD}Select a lab from ${CYAN}$COURSE${NC}${BOLD}:${NC}"
    LAB_ARRAY=()
    j=1
    for l in "./courses/$COURSE"/*/; do
        if [ -d "$l" ]; then
            lname=$(basename "$l")
            if [[ "$lname" == "offline_files" || "$lname" == "cppreference" ]]; then continue; fi
            LAB_ARRAY+=("$lname")

            q_count=0
            if [ -d "$l/testcases" ]; then
                q_count=$(find "$l/testcases" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
            fi
            echo -e "  ${CYAN}${BOLD}$j)${NC} $lname ${DIM}($q_count questions)${NC}"
            ((j++))
        fi
    done

    if [ ${#LAB_ARRAY[@]} -eq 0 ]; then
        echo ""
        print_fail "No labs found for $COURSE."
        echo -e "  ${DIM}Run ${BOLD}./create_lab.sh${NC}${DIM} first.${NC}"
        exit 1
    fi

    echo ""
    read -p "$(echo -e "${YELLOW}Enter choice (1-$((j-1))): ${NC}")" LAB_CHOICE
    if [[ "$LAB_CHOICE" =~ ^[0-9]+$ ]] && [ "$LAB_CHOICE" -ge 1 ] && [ "$LAB_CHOICE" -lt "$j" ]; then
        LAB=${LAB_ARRAY[$((LAB_CHOICE-1))]}
    else
        print_fail "Invalid choice."; exit 1
    fi
fi

COURSE_DIR="./courses/$COURSE"
LAB_DIR="$COURSE_DIR/$LAB"

if [ ! -d "$COURSE_DIR" ]; then
    print_fail "Course '$COURSE' not found in ./courses/"; exit 1
fi
if [ ! -d "$LAB_DIR" ]; then
    print_fail "Lab '$LAB' not found in $COURSE_DIR/"; exit 1
fi

# ---- PRE-LAUNCH SUMMARY ----
STUDENT_COUNT=0
if [ -f "$COURSE_DIR/students.txt" ]; then
    STUDENT_COUNT=$(grep -c '[^[:space:]]' "$COURSE_DIR/students.txt" 2>/dev/null || true)
    [ -z "$STUDENT_COUNT" ] && STUDENT_COUNT=0
fi

QUESTION_COUNT=0
TOTAL_TESTCASES=0
if [ -d "$LAB_DIR/testcases" ]; then
    QUESTION_COUNT=$(find "$LAB_DIR/testcases" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
    TOTAL_TESTCASES=$(find "$LAB_DIR/testcases" -name "input*.txt" 2>/dev/null | wc -l | tr -d ' ')
fi

CONF_INFO=""
if [ -f "$LAB_DIR/course.conf" ]; then
    MARKS=$(grep "^fm_list=" "$LAB_DIR/course.conf" 2>/dev/null | cut -d'=' -f2)
    TIMEOUTS=$(grep "^timeouts=" "$LAB_DIR/course.conf" 2>/dev/null | cut -d'=' -f2)
    [ -n "$MARKS" ] && CONF_INFO="Marks: $MARKS"
    [ -n "$TIMEOUTS" ] && CONF_INFO="$CONF_INFO  |  Timeouts: ${TIMEOUTS}s"
fi

echo -e "\n${BOLD}----------- Pre-Launch Summary -----------${NC}"
echo -e "  Course:      ${CYAN}$COURSE${NC}"
echo -e "  Lab:         ${CYAN}$LAB${NC}"
echo -e "  Students:    $STUDENT_COUNT enrolled"
echo -e "  Questions:   $QUESTION_COUNT questions, $TOTAL_TESTCASES test cases"
if [ -n "$CONF_INFO" ]; then
    echo -e "  Config:      $CONF_INFO"
fi
echo -e "  Port:        $PORT"
echo -e "${BOLD}------------------------------------------${NC}"

if [ "$STUDENT_COUNT" -eq 0 ]; then
    print_warn "students.txt is empty — no students will be able to register!"
    echo -e "  ${DIM}Add roll numbers to: $COURSE_DIR/students.txt${NC}"
fi

if [ "$TOTAL_TESTCASES" -eq 0 ]; then
    print_warn "No test cases found — submissions won't be graded!"
    echo -e "  ${DIM}Add input/output files to: $LAB_DIR/testcases/Q*/input/ and output/${NC}"
fi

echo ""
read -p "$(echo -e "${YELLOW}${BOLD}Start the server? [Y/n]: ${NC}")" CONFIRM
if [[ "$CONFIRM" =~ ^[nN] ]]; then
    echo -e "${DIM}Cancelled.${NC}"
    exit 0
fi

export PATH="$HOME/.local/bin:$PATH"

# ---- STEP 1: INSTALL DEPENDENCIES ----
echo -e "\n${DIM}Setting up...${NC}"

IS_DEBIAN=false
if [ -f /etc/debian_version ]; then IS_DEBIAN=true; fi

if [ "$IS_DEBIAN" = true ]; then
    MISSING_SYS_DEPS=""
    command -v redis-server &> /dev/null || MISSING_SYS_DEPS="$MISSING_SYS_DEPS redis-server"
    command -v bwrap &> /dev/null || MISSING_SYS_DEPS="$MISSING_SYS_DEPS bubblewrap"
    command -v pip3 &> /dev/null || MISSING_SYS_DEPS="$MISSING_SYS_DEPS python3-pip"
    command -v lsof &> /dev/null || MISSING_SYS_DEPS="$MISSING_SYS_DEPS lsof"
    command -v nc &> /dev/null || MISSING_SYS_DEPS="$MISSING_SYS_DEPS netcat-openbsd"
    dpkg -s python3-venv &> /dev/null || MISSING_SYS_DEPS="$MISSING_SYS_DEPS python3-venv python3-full"
    
    if [ -n "$MISSING_SYS_DEPS" ]; then
        log "Installing:$MISSING_SYS_DEPS"
        sudo apt-get update &> /dev/null && sudo apt-get install -y $MISSING_SYS_DEPS &> /dev/null || { print_fail "Failed to install system packages."; exit 1; }
        log_ok "System packages installed"
    fi
fi

if [ "$IS_DEBIAN" = true ]; then
    if ! command -v celery &> /dev/null || ! command -v uvicorn &> /dev/null; then
        pip3 install -r requirements.txt --break-system-packages &> /dev/null || { print_fail "Failed to install Python packages."; exit 1; }
        log_ok "Python packages installed"
    fi
else
    VENV_PATH="$HOME/serverenv"
    if [ ! -d "$VENV_PATH" ]; then
        python3 -m venv "$VENV_PATH" || { print_fail "Failed to create venv."; exit 1; }
        log_ok "Created virtualenv at $VENV_PATH"
    fi
    source "$VENV_PATH/bin/activate"
    if ! command -v celery &> /dev/null; then
        pip install -r requirements.txt &> /dev/null || { print_fail "Failed to install requirements."; exit 1; }
        log_ok "Python packages installed"
    fi
fi

# Sanity checks
for cmd in redis-server celery uvicorn; do
    if ! command -v $cmd &> /dev/null; then
        print_fail "Required command '$cmd' not found after installation!"
        exit 1
    fi
done

# ---- STEP 2: SET UP .active_lab SYMLINKS ----
log "Setting up symlinks..."

mkdir -p .active_lab
mkdir -p logs
mkdir -p "$LAB_DIR/submissions"

for item in students.txt; do
    rm -rf "./.active_lab/$item" 2>/dev/null
    if [ -f "$COURSE_DIR/$item" ]; then ln -sf "../$COURSE_DIR/$item" "./.active_lab/$item"; fi
done

if [ -d "./cppreference" ]; then
    rm -rf "./.active_lab/cppreference" 2>/dev/null
    ln -sf "../cppreference" "./.active_lab/cppreference"
fi

rm -rf "./.active_lab/offline_files" 2>/dev/null
ln -sf "../$COURSE_DIR/offline_files" "./.active_lab/offline_files"

for item in registrations.csv violations.csv grades.csv; do
    if [ ! -f "$LAB_DIR/$item" ]; then touch "$LAB_DIR/$item"; fi
done
 
for item in testcases submissions registrations.csv violations.csv course.conf grades.csv statics; do
    rm -rf "./.active_lab/$item" 2>/dev/null
    if [ -e "$LAB_DIR/$item" ]; then ln -sf "../$LAB_DIR/$item" "./.active_lab/$item"; fi
done

log_ok "Symlinks ready"

# ---- STEP 3: DETECT NETWORK & INJECT SERVER URL ----
log "Detecting network..."

LOCAL_IP=""
[ -z "$LOCAL_IP" ] && LOCAL_IP=$(python3 -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2>/dev/null)
[ -z "$LOCAL_IP" ] && LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null)
[ -z "$LOCAL_IP" ] && LOCAL_IP=$(ipconfig getifaddr en1 2>/dev/null)
[ -z "$LOCAL_IP" ] && LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$LOCAL_IP" ] && LOCAL_IP=$(ifconfig 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | head -1 | awk '{print $2}' | sed 's/addr://')
[ -z "$LOCAL_IP" ] && LOCAL_IP="0.0.0.0"
SERVER_URL="http://${LOCAL_IP}:${PORT}"

log_ok "Server IP: ${LOCAL_IP}"

for cfg in $(find ./.active_lab/statics -name "config.sh" 2>/dev/null); do
    python3 -c "import sys; lines = [l for l in open(sys.argv[1]) if not l.startswith('SERVER_URL=')]; open(sys.argv[1], 'w').writelines(lines)" "$cfg"
    echo "SERVER_URL=\"${SERVER_URL}\"" >> "$cfg"
done
log_ok "Server URL injected"

# ---- STEP 4: PACKAGE STARTER KIT ----
log "Packaging starter kit..."

(
    cd .active_lab/statics || exit
    TARGET_DIR=$(find . -maxdepth 1 -type d ! -name "." ! -name "cppreference" ! -name ".*" | head -n 1 | sed 's|^./||')
    if [ -n "$TARGET_DIR" ]; then
        find "$TARGET_DIR" -type d \( -name "logs" -o -name "actual_output" -o -name "__pycache__" \) -exec rm -rf {} + 2>/dev/null
        rm -f *.zip .*.zip
        chmod +x "$TARGET_DIR"/*.sh 2>/dev/null || true
        python3 -c "import shutil; shutil.make_archive('${TARGET_DIR}', 'zip', '.', '${TARGET_DIR}')"
        mv "${TARGET_DIR}.zip" ".${TARGET_DIR}.zip" 2>/dev/null
    fi
)

ZIP_NAME=$(find .active_lab/statics/ -maxdepth 1 -name ".*.zip" 2>/dev/null | head -1)
if [ -n "$ZIP_NAME" ]; then
    log_ok "Starter kit packaged"
else
    print_warn "No starter kit zip created — check statics/ directory"
fi

# ---- STEP 5: START REDIS ----
log "Starting Redis..."

PORT_6379_IN_USE=false
if command -v lsof &>/dev/null && lsof -i:6379 &>/dev/null; then PORT_6379_IN_USE=true;
elif command -v nc &>/dev/null && nc -z localhost 6379 &>/dev/null; then PORT_6379_IN_USE=true;
fi

if [ "$PORT_6379_IN_USE" = false ]
then
    redis-server --dir logs/ &> logs/redis.log &
    REDIS_PID=$!
    sleep 2
    if ! kill -0 $REDIS_PID 2>/dev/null; then
        print_fail "Redis failed to start! Check logs/redis.log"
        exit 1
    fi
    log_ok "Redis started (PID: $REDIS_PID)"
else
    REDIS_PID=""
    log_ok "Using existing Redis"
fi

# ---- STEP 6: START CELERY WORKERS ----
log "Starting Celery..."

CPU_CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
CELERY_CONCURRENCY=$(( CPU_CORES * 3 ))
[ "$CELERY_CONCURRENCY" -lt 8 ] && CELERY_CONCURRENCY=8
celery -A task.capp worker -n worker_$PORT@%h --loglevel=info -c $CELERY_CONCURRENCY &> logs/celery.log &
CELERY_PID=$!
sleep 1

if kill -0 $CELERY_PID 2>/dev/null; then
    log_ok "Celery started: $CELERY_CONCURRENCY workers"
else
    print_fail "Celery failed to start! Check logs/celery.log"
    tail -5 logs/celery.log
    exit 1
fi

# ---- STEP 7: START FASTAPI SERVER ----
log "Starting FastAPI..."

python3 -m uvicorn main:app --host 0.0.0.0 --port $PORT --limit-concurrency 150 &> logs/fastapi.log &
FASTAPI_PID=$!
sleep 2

if ! kill -0 $FASTAPI_PID 2>/dev/null; then
    print_fail "FastAPI failed to start! Check logs/fastapi.log"
    tail -5 logs/fastapi.log
    exit 1
fi

# ---- SERVER IS LIVE ----
echo ""
echo -e "${GREEN}${BOLD}=======================================${NC}"
echo -e "${GREEN}${BOLD} Server is live! ($COURSE / $LAB)${NC}"
echo -e "${GREEN}${BOLD}=======================================${NC}"
echo -e "  URL:       ${BOLD}http://${LOCAL_IP}:$PORT${NC}"
echo -e "  Students:  $STUDENT_COUNT enrolled"
echo -e "${GREEN}${BOLD}=======================================${NC}"
echo ""
echo -e "Press ${RED}${BOLD}[CTRL+C]${NC} to shut down all services."
echo -e "${DIM}Logs: logs/fastapi.log  |  logs/celery.log  |  logs/redis.log${NC}"

# ---- GRACEFUL SHUTDOWN HANDLER ----
cleanup() {
    local exit_code=$1
    echo -e "\n\n${YELLOW}${BOLD}Shutting down...${NC}"
    if [ "$exit_code" != "0" ] && [ -n "$exit_code" ]; then
        echo -e "${RED}Service failure detected — shutting down all services.${NC}"
    fi
    kill $FASTAPI_PID $CELERY_PID 2>/dev/null
    [ -n "$REDIS_PID" ] && kill $REDIS_PID 2>/dev/null
    pkill -P $$ 2>/dev/null
    echo -e "${GREEN}All services stopped.${NC}"
    exit ${exit_code:-0}
}

trap 'cleanup 0' INT TERM

# ---- WATCHDOG LOOP ----
while true; do
    if [ -n "$REDIS_PID" ] && ! kill -0 $REDIS_PID 2>/dev/null; then
        echo -e "\n${RED}${BOLD}CRITICAL: Redis has crashed!${NC}"
        tail -n 10 logs/redis.log
        cleanup 1
    fi
    if ! kill -0 $CELERY_PID 2>/dev/null; then
        echo -e "\n${RED}${BOLD}CRITICAL: Celery worker has crashed!${NC}"
        tail -n 10 logs/celery.log
        cleanup 1
    fi
    if ! kill -0 $FASTAPI_PID 2>/dev/null; then
        echo -e "\n${RED}${BOLD}CRITICAL: FastAPI server has crashed!${NC}"
        tail -n 10 logs/fastapi.log
        cleanup 1
    fi
    sleep 2
done
