#!/bin/bash
# create_lab.sh - interactive wizard to set up a new lab under a course
# creates the directory structure, starter files, and course.conf

# Terminal colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

print_ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
print_warn() { echo -e "  ${YELLOW}[!!]${NC} $1"; }
print_fail() { echo -e "  ${RED}[FAIL]${NC} $1"; }

# Find the project root (same directory as this script)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$PROJECT_ROOT/start.sh" ]; then
    print_fail "Cannot find the project root. Run from the DCF_Server directory."
    exit 1
fi
cd "$PROJECT_ROOT"

echo ""
echo -e "${BOLD}=======================================${NC}"
echo -e "${BOLD}        Lab Generator Wizard           ${NC}"
echo -e "${BOLD}=======================================${NC}"
echo ""

# ---- COURSE SELECTION ----
echo -e "${BOLD}Select an existing Course or create a new one:${NC}"
COURSE_ARRAY=()
i=1
if [ -d "./courses" ]; then
    for c in ./courses/*/; do
        if [ -d "$c" ]; then
            cname=$(basename "$c")
            COURSE_ARRAY+=("$cname")
            lab_count=$(find "$c" -maxdepth 1 -mindepth 1 -type d ! -name "offline_files" ! -name "cppreference" 2>/dev/null | wc -l | tr -d ' ')
            echo -e "  ${CYAN}$i)${NC} $cname ${DIM}($lab_count labs)${NC}"
            ((i++))
        fi
    done
fi
echo -e "  ${CYAN}N)${NC} Create a New Course"

echo ""
read -p "$(echo -e "${YELLOW}Enter choice (1-$((i-1)) or N): ${NC}")" COURSE_CHOICE

if [[ "$COURSE_CHOICE" == "N" || "$COURSE_CHOICE" == "n" ]]; then
    read -p "$(echo -e "${YELLOW}Enter the new Course Name (e.g., CS2810): ${NC}")" COURSE
    if [ -z "$COURSE" ]; then
        print_fail "Course name cannot be empty."
        exit 1
    fi
elif [[ "$COURSE_CHOICE" =~ ^[0-9]+$ ]] && [ "$COURSE_CHOICE" -ge 1 ] && [ "$COURSE_CHOICE" -lt "$i" ]; then
    COURSE=${COURSE_ARRAY[$((COURSE_CHOICE-1))]}
else
    print_fail "Invalid choice."
    exit 1
fi

echo -e "  Selected: ${CYAN}$COURSE${NC}"

# ---- LAB CONFIGURATION ----
echo ""
read -p "$(echo -e "${YELLOW}Lab Name (e.g., Lab7, MidSem, EndSem): ${NC}")" LAB
if [ -z "$LAB" ]; then
    print_fail "Lab name cannot be empty."
    exit 1
fi

read -p "$(echo -e "${YELLOW}Number of questions (e.g., 3): ${NC}")" NUM_QUESTIONS
if ! [[ "$NUM_QUESTIONS" =~ ^[0-9]+$ ]] || [ "$NUM_QUESTIONS" -lt 1 ]; then
    print_fail "Invalid number of questions."
    exit 1
fi

COURSE_DIR="$PROJECT_ROOT/courses/$COURSE"
LAB_DIR="$COURSE_DIR/$LAB"

# Don't overwrite an existing lab
if [ -d "$LAB_DIR" ]; then
    print_fail "Lab '$LAB' already exists under $COURSE. Delete it first or pick a different name."
    exit 1
fi

# ---- PER-QUESTION SETUP ----
echo ""
echo -e "${BOLD}Configure each question:${NC}"

FM_LIST=""
TIMEOUTS=""
BASH_FM_LIST=""
TOTAL_M=0
for ((q=1; q<=NUM_QUESTIONS; q++))
do
    echo -e "\n  ${CYAN}--- Q$q ---${NC}"
    read -p "$(echo -e "  ${YELLOW}Marks for Q$q (default 50): ${NC}")" Q_MARK
    if ! [[ "$Q_MARK" =~ ^[0-9]+$ ]] || [ "$Q_MARK" -lt 0 ]; then
        Q_MARK=50
        echo -e "  ${DIM}Using default: 50${NC}"
    fi
    
    read -p "$(echo -e "  ${YELLOW}Timeout for Q$q in seconds (default 2): ${NC}")" Q_TIME
    if ! [[ "$Q_TIME" =~ ^[0-9]+$ ]] || [ "$Q_TIME" -lt 1 ]; then
        Q_TIME=2
        echo -e "  ${DIM}Using default: 2s${NC}"
    fi

    if [ $q -eq 1 ]; then
        FM_LIST="$Q_MARK"
        TIMEOUTS="$Q_TIME"
        BASH_FM_LIST="$Q_MARK"
        TOTAL_M="$Q_MARK"
    else
        FM_LIST="$FM_LIST,$Q_MARK"
        TIMEOUTS="$TIMEOUTS,$Q_TIME"
        BASH_FM_LIST="$BASH_FM_LIST $Q_MARK"
        TOTAL_M=$((TOTAL_M + Q_MARK))
    fi
done

# ---- CONFIRMATION ----
echo ""
echo -e "${BOLD}----------- Summary -----------${NC}"
echo -e "  Course:      ${CYAN}$COURSE${NC}"
echo -e "  Lab:         ${CYAN}$LAB${NC}"
echo -e "  Questions:   $NUM_QUESTIONS"
echo -e "  Marks:       $FM_LIST (total: $TOTAL_M)"
echo -e "  Timeouts:    ${TIMEOUTS}s"
echo -e "${BOLD}-------------------------------${NC}"

echo ""
read -p "$(echo -e "${YELLOW}Create this lab? [Y/n]: ${NC}")" CONFIRM
if [[ "$CONFIRM" =~ ^[nN] ]]; then
    echo -e "${DIM}Cancelled.${NC}"
    exit 0
fi

# ---- CREATE EVERYTHING ----
echo ""
echo -e "${BLUE}--->${NC} ${BOLD}Creating directory structure${NC}"

# Create course directory if new
if [ ! -d "$COURSE_DIR" ]; then
    mkdir -p "$COURSE_DIR"
    mkdir -p "$COURSE_DIR/offline_files"
    print_ok "Created new course: $COURSE"
fi

# Submissions directory
mkdir -p "$LAB_DIR/submissions"

# Empty data files
touch "$LAB_DIR/registrations.csv"
touch "$LAB_DIR/violations.csv"
touch "$LAB_DIR/grades.csv"

# Starter kit template
STATICS_DIR="$LAB_DIR/statics/${LAB}"
mkdir -p "$STATICS_DIR/testcases"
mkdir -p "$STATICS_DIR/CS2XBXXX"

# Create testcase directories and starter C++ files
for ((q=1; q<=NUM_QUESTIONS; q++))
do
    mkdir -p "$LAB_DIR/testcases/Q$q/input"
    mkdir -p "$LAB_DIR/testcases/Q$q/output"
    mkdir -p "$STATICS_DIR/testcases/Q$q/input"
    mkdir -p "$STATICS_DIR/testcases/Q$q/output"
    
    cat <<EOF > "$STATICS_DIR/CS2XBXXX/Q$q.cpp"
#include <iostream>

using namespace std;

int main() {
    // Write your code for Q$q here
    
    return 0;
}
EOF
done

print_ok "Created $NUM_QUESTIONS question directories + starter files"

# Write course.conf
echo -e "\n${BLUE}--->${NC} ${BOLD}Writing configuration${NC}"

cat <<EOF > "$LAB_DIR/course.conf"
# Course Configuration for $COURSE
# Marks per question (comma-separated, one per Q)
fm_list=$FM_LIST
# Timeout in seconds per question
timeouts=$TIMEOUTS
EOF
print_ok "Wrote course.conf (marks: $FM_LIST, timeouts: $TIMEOUTS)"

# Create students.txt if missing
if [ ! -f "$COURSE_DIR/students.txt" ]; then
    touch "$COURSE_DIR/students.txt"
    print_ok "Created empty students.txt"
else
    STUDENT_COUNT=$(grep -c '[^[:space:]]' "$COURSE_DIR/students.txt" 2>/dev/null || echo 0)
    print_ok "students.txt already exists ($STUDENT_COUNT students)"
fi

# ---- COPY DEFAULT SCRIPTS ----
echo -e "\n${BLUE}--->${NC} ${BOLD}Copying starter scripts${NC}"

SRC_DIR="$PROJECT_ROOT/default_scripts"
if [ -d "$SRC_DIR" ]; then
    cp "$SRC_DIR/submit.sh" "$STATICS_DIR/" 2>/dev/null || true
    cp "$SRC_DIR/check.sh" "$STATICS_DIR/" 2>/dev/null || true
    cp "$SRC_DIR/config.sh" "$STATICS_DIR/" 2>/dev/null || true
    cp "$SRC_DIR/README.md" "$STATICS_DIR/" 2>/dev/null || true
    
    # Patch submit.sh with this lab's question count and marks
    if [ -f "$STATICS_DIR/submit.sh" ]; then
        sed -i.bak "s/^TOTAL_QUESTIONS=.*/TOTAL_QUESTIONS=$NUM_QUESTIONS/" "$STATICS_DIR/submit.sh"
        sed -i.bak "s/^FULL_MARKS=(.*)/FULL_MARKS=($BASH_FM_LIST)/" "$STATICS_DIR/submit.sh"
        sed -i.bak "s/^_FULL_MARKS=.*/_FULL_MARKS=$TOTAL_M/" "$STATICS_DIR/submit.sh"
        rm -f "$STATICS_DIR/submit.sh.bak" 2>/dev/null
    fi
    print_ok "Copied submit.sh, check.sh, config.sh, README.md"
else
    print_warn "default_scripts/ directory not found — no starter scripts copied"
fi

# ---- DONE ----
echo ""
echo -e "${GREEN}${BOLD}=======================================${NC}"
echo -e "${GREEN}${BOLD} Created $COURSE / $LAB successfully!${NC}"
echo -e "${GREEN}${BOLD}=======================================${NC}"
echo ""
echo -e "${BOLD}What to do next:${NC}"
echo ""
echo -e "  1. Add your question paper PDF to:"
echo -e "     ${CYAN}$STATICS_DIR/${NC}"
echo ""
echo -e "  2. Add private test cases (for server grading) to:"
for ((q=1; q<=NUM_QUESTIONS; q++)); do
    echo -e "     ${CYAN}$LAB_DIR/testcases/Q$q/input/${NC} and ${CYAN}.../output/${NC}"
done
echo ""
echo -e "  3. Add public test cases (for students) to:"
for ((q=1; q<=NUM_QUESTIONS; q++)); do
    echo -e "     ${CYAN}$STATICS_DIR/testcases/Q$q/input/${NC} and ${CYAN}.../output/${NC}"
done
echo ""
echo -e "  4. Add student roll numbers to:"
echo -e "     ${CYAN}$COURSE_DIR/students.txt${NC}"
echo ""
echo -e "  5. When ready, start the server:"
echo -e "     ${GREEN}./start.sh $COURSE $LAB${NC}"
echo ""
