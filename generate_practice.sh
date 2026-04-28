#!/bin/bash
# generate_practice.sh - creates a standalone practice zip for students
# merges public + private test cases, generates practice.sh and practice.bat

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

print_ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
print_warn() { echo -e "  ${YELLOW}[!!]${NC} $1"; }
print_fail() { echo -e "  ${RED}[FAIL]${NC} $1"; }

# Find the project root (same directory as this script)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$PROJECT_ROOT/start.sh" ]; then
    print_fail "Cannot find the project root."
    exit 1
fi
cd "$PROJECT_ROOT"

echo ""
echo -e "${BOLD}=======================================${NC}"
echo -e "${BOLD}    Practice Kit Generator             ${NC}"
echo -e "${BOLD}=======================================${NC}"
echo ""

# ---- COURSE SELECTION ----
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
    print_fail "No courses found."
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

COURSE_DIR="$PROJECT_ROOT/courses/$COURSE"

# ---- LAB SELECTION ----
echo ""
echo -e "${BOLD}Select a lab from $COURSE:${NC}"
LAB_ARRAY=()
j=1
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
    print_fail "No labs found in $COURSE."
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

LAB_DIR="$COURSE_DIR/$LAB"
STATICS_DIR="$LAB_DIR/statics/$LAB"

if [ ! -d "$STATICS_DIR" ]; then
    print_fail "$STATICS_DIR not found. Is this a valid lab?"
    exit 1
fi

# ---- COUNT WHAT WE'RE WORKING WITH ----
Q_COUNT=0
PRIVATE_TC=0
PUBLIC_TC=0
if [ -d "$LAB_DIR/testcases" ]; then
    Q_COUNT=$(find "$LAB_DIR/testcases" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
    PRIVATE_TC=$(find "$LAB_DIR/testcases" -name "input*.txt" 2>/dev/null | wc -l | tr -d ' ')
fi
if [ -d "$STATICS_DIR/testcases" ]; then
    PUBLIC_TC=$(find "$STATICS_DIR/testcases" -name "input*.txt" 2>/dev/null | wc -l | tr -d ' ')
fi

echo ""
echo -e "${BOLD}----------- Summary -----------${NC}"
echo -e "  Course:          $COURSE / $LAB"
echo -e "  Questions:       $Q_COUNT"
echo -e "  Private tests:   $PRIVATE_TC"
echo -e "  Public tests:    $PUBLIC_TC"
echo -e "  Total in kit:    $((PRIVATE_TC + PUBLIC_TC)) test cases"
echo -e "${BOLD}-------------------------------${NC}"

echo ""
read -p "$(echo -e "${YELLOW}Generate practice kit? [Y/n]: ${NC}")" CONFIRM
if [[ "$CONFIRM" =~ ^[nN] ]]; then
    echo -e "${DIM}Cancelled.${NC}"
    exit 0
fi

# ---- CREATE EXPORT DIRECTORY ----
EXPORT_ROOT="$PROJECT_ROOT/practice_exports"
PRACTICE_DIR="$EXPORT_ROOT/${COURSE}_${LAB}_Practice"

mkdir -p "$EXPORT_ROOT"
rm -rf "$PRACTICE_DIR"
mkdir -p "$PRACTICE_DIR"

# ---- COPY STARTER FILES ----
echo ""
echo -e "${BLUE}--->${NC} ${BOLD}Copying starter files${NC}"
cp "$STATICS_DIR/CS2XBXXX/"*.cpp "$PRACTICE_DIR/" 2>/dev/null || true
cp "$STATICS_DIR/"*.pdf "$PRACTICE_DIR/" 2>/dev/null || true

# Export offline datasets so the practice executable can find them natively
for d_name in "csv" "assets" "data" "public" "private"; do
    if [ -d "$LAB_DIR/$d_name" ]; then
        cp -r "$LAB_DIR/$d_name" "$PRACTICE_DIR/" 2>/dev/null || true
    fi
done

print_ok "Copied .cpp templates, question PDFs, and offline datasets"

# ---- MERGE TESTCASES ----
echo -e "\n${BLUE}--->${NC} ${BOLD}Merging test cases${NC}"
mkdir -p "$PRACTICE_DIR/testcases"

for q_dir in "$LAB_DIR/testcases"/Q*; do
    if [ ! -d "$q_dir" ]; then continue; fi
    qname=$(basename "$q_dir")
    
    mkdir -p "$PRACTICE_DIR/testcases/$qname/input"
    mkdir -p "$PRACTICE_DIR/testcases/$qname/output"
    
    # Copy public testcases with _pub suffix to avoid overwriting private ones
    if [ -d "$STATICS_DIR/testcases/$qname/input" ]; then
        for pub_in in "$STATICS_DIR/testcases/$qname/input/"*.txt; do
            [ -f "$pub_in" ] || continue
            bname=$(basename "$pub_in" .txt)
            num="${bname#input}"
            cp "$pub_in" "$PRACTICE_DIR/testcases/$qname/input/input${num}_pub.txt" 2>/dev/null || true
            
            pub_out="$STATICS_DIR/testcases/$qname/output/output${num}.txt"
            if [ -f "$pub_out" ]; then
                cp "$pub_out" "$PRACTICE_DIR/testcases/$qname/output/output${num}_pub.txt" 2>/dev/null || true
            fi
        done
    fi
    
    # Copy private testcases (names stay as-is)
    cp "$q_dir/input/"*.txt "$PRACTICE_DIR/testcases/$qname/input/" 2>/dev/null || true
    cp "$q_dir/output/"*.txt "$PRACTICE_DIR/testcases/$qname/output/" 2>/dev/null || true
done

MERGED_COUNT=$(find "$PRACTICE_DIR/testcases" -name "input*.txt" 2>/dev/null | wc -l | tr -d ' ')
print_ok "Merged $MERGED_COUNT test cases across $Q_COUNT questions"

# Extract per-question timeouts from course.conf
TIMEOUT_LIST="2"
DEFAULT_TIMEOUT=2
if [ -f "$LAB_DIR/course.conf" ]; then
    T_STR=$(grep "^timeouts=" "$LAB_DIR/course.conf" | cut -d'=' -f2)
    if [ -n "$T_STR" ]; then
        TIMEOUT_LIST="$T_STR"
        DEFAULT_TIMEOUT=$(echo "$T_STR" | rev | cut -d',' -f1 | rev)
    fi
fi

# ---- GENERATE practice.sh ----
echo -e "\n${BLUE}--->${NC} ${BOLD}Generating practice scripts${NC}"

PRACTICE_SCRIPT="$PRACTICE_DIR/practice.sh"

cat << 'EOF' > "$PRACTICE_SCRIPT"
#!/bin/bash
# ==============================================================================
#                  Local Practice Evaluator
# ==============================================================================
# Evaluates your C++ code against ALL merged testcases (public+private).
# Features: Pass/Fail accounting, automatic timeout limits, detailed diffs on failure.
# Notes: MARKS ARE IGNORED.
# ==============================================================================

TESTCASES_DIR="testcases"
ACTUAL_OUTPUT_DIR="actual_output" 
COMPILER="g++"
COMPILER_FLAGS="-std=c++17 -O2"
EOF

# Write per-question timeout list
echo "TIMEOUT_LIST=\"$TIMEOUT_LIST\"" >> "$PRACTICE_SCRIPT"
echo "DEFAULT_TIMEOUT=$DEFAULT_TIMEOUT" >> "$PRACTICE_SCRIPT"

cat << 'EOF' >> "$PRACTICE_SCRIPT"


RED='\033[1;31m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
CYAN='\033[1;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

print_error()   { echo -e "${RED}${BOLD}[ERROR]   $1${NC}"; }
print_success() { echo -e "${GREEN}${BOLD}[SUCCESS] $1${NC}"; }

mkdir -p "$ACTUAL_OUTPUT_DIR"

if [ "$#" -eq 0 ]; then
    shopt -s nullglob
    question_dirs=("$TESTCASES_DIR"/*/)
    shopt -u nullglob
    if [ ${#question_dirs[@]} -eq 0 ]; then
        print_error "No testcases found."
        exit 1
    fi
    questions=()
    for q_dir in "${question_dirs[@]}"; do
        questions+=("$(basename "$q_dir")")
    done
else
    questions=("Q$1")
fi



for question in "${questions[@]}"; do
    echo -e "${YELLOW}${BOLD}=> Evaluating ${question}...${NC}"
    
    mkdir -p "$ACTUAL_OUTPUT_DIR/$question"
    
    submission_file="${question}.cpp"
    if [ ! -f "$submission_file" ]; then
        print_error "Could not find $submission_file in current directory."
        continue
    fi

    executable_file=".${question}_exec"
    
    echo -e "${BLUE}---> Compiling $submission_file...${NC}"
    if ! $COMPILER $COMPILER_FLAGS "$submission_file" -o "$executable_file" 2>/tmp/compile_err; then
        print_error "Compilation failed for $submission_file"
        cat /tmp/compile_err
        continue
    fi

    # Look up per-question timeout from TIMEOUT_LIST
    q_num=${question#Q}
    IFS=',' read -ra _timeouts <<< "$TIMEOUT_LIST"
    if [ "$q_num" -ge 1 ] 2>/dev/null && [ "$q_num" -le "${#_timeouts[@]}" ]; then
        TIMEOUT_SECONDS=${_timeouts[$((q_num-1))]}
    else
        TIMEOUT_SECONDS=$DEFAULT_TIMEOUT
    fi

    echo -e "${BLUE}---> Running test cases (Timeout: ${TIMEOUT_SECONDS}s)...${NC}\n"

    total_tests=0
    passed_tests=0

    for input_file in "$TESTCASES_DIR/$question"/input/*.txt; do
        [ -f "$input_file" ] || continue
        total_tests=$((total_tests + 1))
        
        test_case_name=$(basename "$input_file" .txt)
        test_number="${test_case_name#input}"
        expected_output_file="$TESTCASES_DIR/$question/output/output${test_number}.txt"
        student_output_file="$ACTUAL_OUTPUT_DIR/$question/output${test_number}.txt"

        if [ ! -f "$expected_output_file" ]; then
            printf "    %-20s %b\n" "${test_case_name}:" "${DIM}[SKIPPED - Missing Expected Output]${NC}"
            continue
        fi

        rm -f "$student_output_file"

        # Portable timeout execution
        if command -v timeout &>/dev/null; then
            timeout "$TIMEOUT_SECONDS" ./"$executable_file" < "$input_file" > "$student_output_file"
            exit_code=$?
        else
            # Native bash fallback for macOS / environments missing 'timeout' utility
            ./"$executable_file" < "$input_file" > "$student_output_file" &
            pid=$!
            count=0
            while kill -0 $pid 2>/dev/null && [ $count -lt $TIMEOUT_SECONDS ]; do
                sleep 1
                count=$((count+1))
            done
            
            if kill -0 $pid 2>/dev/null; then
                kill -9 $pid 2>/dev/null
                exit_code=124  # Force timeout exit code
            else
                wait $pid
                exit_code=$?
            fi
        fi

        if [ $exit_code -eq 124 ] || [ $exit_code -eq 142 ]; then
            printf "    %-20s [%b]\n" "${test_case_name}" "${YELLOW}${BOLD}Time Limit Exceeded${NC}"
        elif [ $exit_code -ne 0 ]; then
            printf "    %-20s [%b]\n" "${test_case_name}" "${RED}${BOLD}Runtime Error${NC}"
        else
            if diff -w -B "$student_output_file" "$expected_output_file" > /dev/null 2>&1; then
                printf "    %-20s [%b]\n" "${test_case_name}" "${GREEN}${BOLD}Passed${NC}"
                passed_tests=$((passed_tests + 1))
            else
                printf "    %-20s [%b]\n" "${test_case_name}" "${RED}${BOLD}Wrong Answer${NC}"
            fi
        fi
    done
    
    rm -f "$executable_file"

    failed_tests=$((total_tests - passed_tests))
    
    # Summary
    echo ""
    echo -e "    ----------------------------------------"
    printf  "    PRACTICE SUMMARY: ${BOLD}%s${NC}\n" "${question}"
    echo -e "    ----------------------------------------"
    printf  "    Total Cases:  %s\n" "${total_tests}"
    printf  "    Passed:       ${GREEN}%s${NC}\n" "${passed_tests}"
    printf  "    Failed:       ${RED}%s${NC}\n" "${failed_tests}"
    echo -e "    ----------------------------------------"
    
    if [ "$failed_tests" -gt 0 ]; then
        echo -e "    ${DIM}Your actual output is saved in 'actual_output/${question}/'${NC}\n"
    else
        echo ""
    fi
done

echo -e "${GREEN}${BOLD}======================================================================${NC}"
echo -e "${GREEN}${BOLD} Local checking complete. Keep practicing! ${NC}"
echo -e "${GREEN}${BOLD}======================================================================${NC}\n"
EOF

chmod +x "$PRACTICE_SCRIPT"
print_ok "Generated practice.sh"

# ---- GENERATE practice.bat (Windows) ----

BAT_SCRIPT="$PRACTICE_DIR/practice.bat"

echo "@echo off" > "$BAT_SCRIPT"
echo "setlocal enabledelayedexpansion" >> "$BAT_SCRIPT"
echo "set TIMEOUT_LIST=$TIMEOUT_LIST" >> "$BAT_SCRIPT"
echo "set DEFAULT_TIMEOUT=$DEFAULT_TIMEOUT" >> "$BAT_SCRIPT"

cat << 'EOF' >> "$BAT_SCRIPT"

echo.
echo ======================================================================
echo  DCF Offline Practice Mode (Windows)
echo ======================================================================

set COMPILER=g++
set COMPILER_FLAGS=-std=c++17 -O2

if "%~1"=="" (
    for /d %%d in (testcases\Q*) do (
        set q_name=%%~nxd
        call :EVALUATE !q_name!
    )
) else (
    call :EVALUATE "Q%~1"
)
echo.
echo ======================================================================
echo  Local checking complete. Keep practicing!
echo ======================================================================
goto :EOF

:EVALUATE
set "question=%~1"
echo.
echo =^> Evaluating %question%...

if not exist "%question%.cpp" (
    echo [ERROR] Could not find %question%.cpp
    goto :EOF
)

rem Look up per-question timeout
set "q_num=%question:Q=%"
set _idx=0
set "TIMEOUT_SECONDS=%DEFAULT_TIMEOUT%"
for %%t in (%TIMEOUT_LIST%) do (
    set /a _idx+=1
    if !_idx!==%q_num% set "TIMEOUT_SECONDS=%%t"
)

if not exist "actual_output\%question%" mkdir "actual_output\%question%"

echo ---^> Compiling %question%.cpp...
%COMPILER% %COMPILER_FLAGS% %question%.cpp -o .%question%_exec.exe >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Compilation failed
    goto :EOF
)

echo ---^> Running test cases (Timeout: !TIMEOUT_SECONDS!s)...
set total_tests=0
set passed_tests=0

for %%i in (testcases\%question%\input\*.txt) do (
    set "test_case_name=%%~ni"
    set "test_number=!test_case_name:input=!"
    set "expected=testcases\%question%\output\output!test_number!.txt"
    set "student=actual_output\%question%\output!test_number!.txt"
    
    set /a total_tests+=1
    
    if not exist "!expected!" (
        echo     !test_case_name!          [SKIPPED - Missing Expected Output]
    ) else (
        powershell -Command "$p = Start-Process -FilePath '.\\.%question%_exec.exe' -RedirectStandardInput '%%i' -RedirectStandardOutput '!student!' -PassThru -WindowStyle Hidden; if ($p.WaitForExit(!TIMEOUT_SECONDS!000)) { exit $p.ExitCode } else { Stop-Process -Id $p.Id -Force; exit 124 }"
        
        if errorlevel 124 (
            echo     !test_case_name!          [Time Limit Exceeded]
        ) else if errorlevel 1 (
            echo     !test_case_name!          [Runtime Error]
        ) else (
            fc /W "!student!" "!expected!" >nul 2>nul
            if errorlevel 1 (
                echo     !test_case_name!          [Wrong Answer]
            ) else (
                echo     !test_case_name!          [Passed]
                set /a passed_tests+=1
            )
        )
    )
)

del .%question%_exec.exe 2>nul
set /a failed_tests=total_tests-passed_tests

echo.
echo     PRACTICE SUMMARY: %question%
echo     ----------------------------------------
echo     Total Cases:  !total_tests!
echo     Passed Tests: !passed_tests!
echo     Failed Tests: !failed_tests!
echo     ----------------------------------------
if !failed_tests! GTR 0 (
    echo     Note: For failed tests, your actual output has been saved in 'actual_output\%question%\'
)

goto :EOF
EOF

print_ok "Generated practice.bat (Windows)"

# ---- GENERATE README.md ----
echo -e "\n${BLUE}--->${NC} ${BOLD}Generating student README${NC}"
cat << 'EOF' > "$PRACTICE_DIR/README.md"
# Practice Kit

This is your standalone practice environment with all test cases (public + private) merged together.

## How to use

Write your code in `Q1.cpp`, `Q2.cpp`, etc. and then run the practice script.

### Mac / Linux

```bash
# Test all questions
./practice.sh

# Test only Q1
./practice.sh 1

# Test only Q2
./practice.sh 2
```

### Windows

```cmd
REM Test all questions
practice.bat

REM Test only Q1
practice.bat 1
```

## What the output means

- **Passed** — your output matches the expected output
- **Wrong Answer** — something's different (check `actual_output/` to see what you printed)
- **Time Limit Exceeded** — your code took too long
- **Runtime Error** — your code crashed (segfault, out-of-bounds, etc.)

## Viewing failures

When a test fails, the script saves what your code actually printed to `actual_output/Q1/`, `actual_output/Q2/`, etc. Open those files and compare them against `testcases/Q1/output/` to see the difference.
EOF
print_ok "Generated README.md"

# ---- ZIP EXPORT ----
echo -e "\n${BLUE}--->${NC} ${BOLD}Creating ZIP${NC}"
cd "$EXPORT_ROOT"
zip -r "${COURSE}_${LAB}_Practice.zip" "${COURSE}_${LAB}_Practice" > /dev/null
cd "$PROJECT_ROOT"

ZIP_SIZE=$(du -h "$EXPORT_ROOT/${COURSE}_${LAB}_Practice.zip" 2>/dev/null | awk '{print $1}')
print_ok "Created ${COURSE}_${LAB}_Practice.zip ($ZIP_SIZE)"

echo ""
echo -e "${GREEN}${BOLD}=======================================${NC}"
echo -e "${GREEN}${BOLD} Practice kit ready!${NC}"
echo -e "${GREEN}${BOLD}=======================================${NC}"
echo ""
echo -e "Share this with your students:"
echo -e "  ${BOLD}$EXPORT_ROOT/${COURSE}_${LAB}_Practice.zip${NC}"
echo ""
