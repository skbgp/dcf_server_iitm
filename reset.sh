#!/bin/bash
# reset.sh - wipes submissions, grades, and registrations for a lab
# usage: ./reset.sh              (interactive)
#        ./reset.sh CS2810 Lab5  (direct)

# Terminal colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

print_ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
print_fail() { echo -e "  ${RED}[FAIL]${NC} $1"; }

# Safety check
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$PROJECT_ROOT/start.sh" ]; then
    print_fail "Cannot find the project root. Run from the DCF_Server directory."
    exit 1
fi
cd "$PROJECT_ROOT"

# ---- COURSE AND LAB SELECTION ----
if [ "$#" -eq 2 ]; then
    COURSE=$1
    LAB=$2
else
    echo ""
    echo -e "${RED}${BOLD}=======================================${NC}"
    echo -e "${RED}${BOLD}           RESET LAB DATA              ${NC}"
    echo -e "${RED}${BOLD}=======================================${NC}"
    echo ""

    echo -e "${BOLD}Select a course:${NC}"
    COURSE_ARRAY=()
    i=1
    if [ -d "./courses" ]; then
        for c in ./courses/*/; do
            if [ -d "$c" ]; then
                cname=$(basename "$c")
                COURSE_ARRAY+=("$cname")
                echo "  $i) $cname"
                ((i++))
            fi
        done
    fi

    if [ ${#COURSE_ARRAY[@]} -eq 0 ]; then
        print_fail "No courses found in ./courses/. Nothing to reset."
        exit 1
    fi

    echo ""
    read -p "$(echo -e "${YELLOW}Enter choice (1-$((i-1))): ${NC}")" COURSE_CHOICE

    if [[ "$COURSE_CHOICE" =~ ^[0-9]+$ ]] && [ "$COURSE_CHOICE" -ge 1 ] && [ "$COURSE_CHOICE" -lt "$i" ]; then
        COURSE=${COURSE_ARRAY[$((COURSE_CHOICE-1))]}
    else
        print_fail "Invalid choice."
        exit 1
    fi

    echo -e "  Selected: $COURSE"
    echo ""

    echo -e "${BOLD}Select a lab to reset:${NC}"
    LAB_ARRAY=()
    j=1
    COURSE_DIR="./courses/$COURSE"
    for l in "$COURSE_DIR"/*/; do
        if [ -d "$l" ]; then
            lname=$(basename "$l")
            if [[ "$lname" == "offline_files" || "$lname" == "cppreference" ]]; then
                continue
            fi
            LAB_ARRAY+=("$lname")
            echo "  $j) $lname"
            ((j++))
        fi
    done

    if [ ${#LAB_ARRAY[@]} -eq 0 ]; then
        print_fail "No labs found for $COURSE. Nothing to reset."
        exit 1
    fi

    echo ""
    read -p "$(echo -e "${YELLOW}Enter choice (1-$((j-1))): ${NC}")" LAB_CHOICE

    if [[ "$LAB_CHOICE" =~ ^[0-9]+$ ]] && [ "$LAB_CHOICE" -ge 1 ] && [ "$LAB_CHOICE" -lt "$j" ]; then
        LAB=${LAB_ARRAY[$((LAB_CHOICE-1))]}
    else
        print_fail "Invalid choice."
        exit 1
    fi
fi

LAB_PATH="courses/$COURSE/$LAB"

if [ ! -d "$LAB_PATH" ]; then
    print_fail "Directory '$LAB_PATH' not found."
    exit 1
fi

# ---- SHOW WHAT WILL BE DELETED ----
SUB_COUNT=0
if [ -d "$LAB_PATH/submissions" ]; then
    SUB_COUNT=$(find "$LAB_PATH/submissions" -type f -name "*.cpp" 2>/dev/null | wc -l | tr -d ' ')
fi

REG_COUNT=0
if [ -f "$LAB_PATH/registrations.csv" ]; then
    REG_COUNT=$(( $(wc -l < "$LAB_PATH/registrations.csv" | tr -d ' ') - 1 ))
    [ "$REG_COUNT" -lt 0 ] && REG_COUNT=0
fi

echo ""
echo -e "${RED}${BOLD}=======================================${NC}"
echo -e "${RED}${BOLD}       WARNING: DESTRUCTIVE RESET      ${NC}"
echo -e "${RED}${BOLD}=======================================${NC}"
echo ""
echo -e "  Course:        $COURSE / $LAB"
echo -e "  Submissions:   $SUB_COUNT files will be deleted"
echo -e "  Registrations: $REG_COUNT entries will be cleared"
echo -e "  Grades:        will be wiped"
echo -e "  Violations:    will be cleared"
echo ""
echo -e "${RED}This cannot be undone.${NC}"
echo ""
read -p "$(echo -e "${YELLOW}${BOLD}Type 'yes' to confirm: ${NC}")" response

if [[ "$response" != "yes" ]]; then
    echo -e "${DIM}Reset aborted.${NC}"
    exit 0
fi

echo ""

# Stop the server first to prevent file lock issues
echo -e "${BLUE}--->${NC} ${BOLD}Stopping server${NC}"
pkill -f "uvicorn main:app" 2>/dev/null && print_ok "Stopped FastAPI" || echo -e "  ${DIM}[--] FastAPI not running${NC}"
pkill -f "celery -A task.capp worker" 2>/dev/null && print_ok "Stopped Celery" || echo -e "  ${DIM}[--] Celery not running${NC}"

# Wipe submissions
echo -e "\n${BLUE}--->${NC} ${BOLD}Clearing data${NC}"
if [ -d "$LAB_PATH/submissions" ]; then
    rm -rf "$LAB_PATH/submissions/"*
    print_ok "Deleted all submissions"
fi

# Reset CSV files to headers only
echo "roll_no,ip_address,timestamp" > "$LAB_PATH/registrations.csv"
print_ok "Reset registrations.csv"

echo "timestamp,violation_type,roll_no,expected_ip,actual_ip,count" > "$LAB_PATH/violations.csv"
print_ok "Reset violations.csv"

# Clear grades but keep the header
> "$LAB_PATH/grades.csv"
print_ok "Reset grades.csv"

# Clear the symlink cache
rm -rf .active_lab/* 2>/dev/null
print_ok "Purged .active_lab/ cache"

echo ""
echo -e "${GREEN}${BOLD}$COURSE / $LAB has been factory reset.${NC}"
echo -e "To restart: ${BOLD}./start.sh $COURSE $LAB${NC}"
echo ""
