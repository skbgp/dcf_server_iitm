#!/bin/bash
# check.sh — compiles and tests your code against sample test cases locally


TESTCASES_DIR="testcases"
ACTUAL_OUTPUT_DIR="actual_output" 
COMPILER="g++"
COMPILER_FLAGS="-std=c++17 -O2"
TIMEOUT_SECONDS=2

# Get absolute path
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"


RED='\033[1;31m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
CYAN='\033[1;36m'
MAGENTA='\033[1;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'


print_header() {
    echo -e "\n${CYAN}${BOLD}======================================================================${NC}"
    echo -e "${CYAN}${BOLD} $1 ${NC}"
    echo -e "${CYAN}${BOLD}======================================================================${NC}\n"
}

print_step()    { echo -e "${BLUE}${BOLD}:: $1${NC}"; }
print_info()    { echo -e "${DIM}[INFO]    $1${NC}"; }
print_success() { echo -e "${GREEN}${BOLD}[SUCCESS] $1${NC}"; }
print_error()   { echo -e "${RED}${BOLD}[ERROR]   $1${NC}"; }
print_warning() { echo -e "${YELLOW}${BOLD}[WARNING] $1${NC}"; }

# --- Portable timeout function ---
run_with_timeout() {
    local secs=$1; shift
    "$@" &
    local pid=$!
    ( sleep "$secs" && kill "$pid" 2>/dev/null ) &
    local watcher=$!
    wait "$pid" 2>/dev/null
    local exit_code=$?
    kill "$watcher" 2>/dev/null
    wait "$watcher" 2>/dev/null
    if [ $exit_code -eq 143 ]; then return 124; fi
    return $exit_code
}

# --- Checking Function ---
grade_student() {
    local student_dir=$1
    local questions=("${@:2}")
    local roll_number=""
    
    # Priority 1: config.sh
    if [ -f "config.sh" ]; then
        ROLL_NO=$(grep '^ROLL_NO=' config.sh | cut -d'"' -f2)
        if [ -n "$ROLL_NO" ] && [[ "$ROLL_NO" != *"XXX"* ]]; then roll_number="$ROLL_NO"; fi
    fi
    
    # Priority 2: Folder name
    if [ -z "$roll_number" ]; then
        local folder_roll=$(basename "$student_dir")
        if [[ "$folder_roll" != "." && "$folder_roll" != "./" && -n "$folder_roll" ]]; then roll_number="$folder_roll"; fi
    fi

    # Priority 3: Current directory name
    if [ -z "$roll_number" ] || [[ "$roll_number" == "." || "$roll_number" == "./" ]]; then
        roll_number=$(basename "$(pwd)")
    fi
    
    print_header "Checking Submissions for: ${MAGENTA}$roll_number"
    
    local student_log_dir="logs"
    mkdir -p "$student_log_dir"

    for question in "${questions[@]}"; do
        echo -e "${YELLOW}${BOLD}:: Evaluating ${question}...${NC}"
        
        rm -rf "$ACTUAL_OUTPUT_DIR/$question" 2>/dev/null
        mkdir -p "$ACTUAL_OUTPUT_DIR/$question"
        
        local log_file="${student_log_dir}/${question}_log.txt"
        echo "Log for $roll_number - Question $question" > "$log_file"

        local total_tests=0
        local passed_tests=0
        local submission_file=""
        local question_digits=$(echo "$question" | tr -dc '0-9')

        shopt -s nullglob
        for potential_file in "${student_dir}"*.cpp; do
            local base_name=$(basename "$potential_file" .cpp)
            local base_name_digits=$(echo "$base_name" | tr -dc '0-9')
            if [[ -n "$base_name_digits" && "$base_name_digits" == "$question_digits" ]]; then
                submission_file="$potential_file"
                break
            fi
        done
        shopt -u nullglob

        if [ -z "$submission_file" ]; then
            print_error "Submission file not found for ${question}."
            echo "Error: Submission file not found." >> "$log_file"
            continue
        fi

        local executable_file="${student_log_dir}/.${roll_number}_${question}_exec"
        
        print_step "Compiling $submission_file..."
        if $COMPILER $COMPILER_FLAGS "$submission_file" -o "$executable_file" >> "$log_file" 2>&1 ; then
            print_success "Compilation successful"
        else
            print_error "Compilation failed"
            echo -e "${DIM}Check logs at: ${log_file}${NC}\n"
            continue
        fi

        trap 'rm -f "${student_log_dir}/.${roll_number}_"*"_exec" 2>/dev/null' EXIT

        print_step "Running test cases..."
        echo ""
        
        for input_file in "$TESTCASES_DIR/$question"/input/*.txt; do
            [ -f "$input_file" ] || continue
            total_tests=$((total_tests + 1))
            local test_case_name=$(basename "$input_file" .txt)
            local test_number="${test_case_name#input}"
            local expected_output_file="$TESTCASES_DIR/$question/output/output${test_number}.txt"
            
            local student_actual_output_dir="$ACTUAL_OUTPUT_DIR/$question"
            local student_output_file=""
            
            student_output_file="$student_actual_output_dir/output${test_number}.txt"

            mkdir -p "$student_actual_output_dir"
            rm -f "$student_output_file"

            if [ ! -f "$expected_output_file" ]; then
                printf "    %-20s %b\n" "${test_case_name}:" "${DIM}[SKIPPED - Missing Output]${NC}"
                continue
            fi

            # Execution with timeout
            if command -v timeout &>/dev/null; then
                timeout "$TIMEOUT_SECONDS" ./"$executable_file" < "$input_file" > "$student_output_file"
                local exit_code=$?
            elif command -v perl &>/dev/null; then
                perl -e '$timeout = shift; $pid = fork; if ($pid == 0) { exec @ARGV } $SIG{ALRM} = sub { kill 9, $pid; exit 124 }; alarm $timeout; wait; alarm 0; exit ($? >> 8)' "$TIMEOUT_SECONDS" ./"$executable_file" < "$input_file" > "$student_output_file" 2>/dev/null
                local exit_code=$?
            else
                ./"$executable_file" < "$input_file" > "$student_output_file"
                local exit_code=$?
            fi

            local VERDICT=""
            local FORMATTED_VERDICT=""
            
            if [ $exit_code -eq 124 ] || [ $exit_code -eq 142 ]; then
                FORMATTED_VERDICT="${YELLOW}${BOLD}Time Limit Exceeded${NC}"
            elif [ $exit_code -ne 0 ]; then
                FORMATTED_VERDICT="${RED}${BOLD}Runtime Error${NC}"
            else
                if diff -w -B "$student_output_file" "$expected_output_file" > /dev/null; then
                    FORMATTED_VERDICT="${GREEN}${BOLD}Passed${NC}"
                    passed_tests=$((passed_tests + 1))
                else
                    FORMATTED_VERDICT="${RED}${BOLD}Wrong Answer${NC}"
                    {
                        echo "--- Failure Details for ${test_case_name} ---"
                        echo "Input:"; head -n 20 "$input_file"
                        echo -e "\nExpected Output:"; head -n 20 "$expected_output_file"
                        echo -e "\nActual Output:"; head -n 20 "$student_output_file"
                        echo -e "\n---------------------------------------\n"
                    } >> "$log_file"
                fi
            fi
            
            
            # Print perfectly aligned result
            printf "    %-20s [%b]\n" "${test_case_name}" "$FORMATTED_VERDICT"
        done
        
        rm -f "$executable_file"

        local failed_tests=$((total_tests - passed_tests))
        
        # Consistent Summary Box
        echo -e "\n    ┌────────────────────────────────────────┐"
        printf  "    │ ${BOLD}%-38s${NC} │\n" "SUMMARY: ${question}"
        echo -e "    ├────────────────────────────────────────┤"
        printf  "    │ %-20s ${CYAN}%-17s${NC} │\n" "Total Cases:" "${total_tests}"
        printf  "    │ %-20s ${GREEN}%-17s${NC} │\n" "Passed Tests:" "${passed_tests}"
        printf  "    │ %-20s ${RED}%-17s${NC} │\n" "Failed Tests:" "${failed_tests}"
        echo -e "    └────────────────────────────────────────┘\n"
        
    done
}

# --- Initialization & Argument Parsing ---
mkdir -p "$ACTUAL_OUTPUT_DIR"
questions=()

if [ "$#" -eq 0 ]; then
    print_info "Checking all available questions..."
    shopt -s nullglob
    question_dirs=("$TESTCASES_DIR"/*/)
    shopt -u nullglob

    if [ ${#question_dirs[@]} -eq 0 ]; then
        print_error "No question directories found in '$TESTCASES_DIR'."
        exit 1
    fi

    for q_dir in "${question_dirs[@]}"; do
        questions+=("$(basename "$q_dir")")
    done

elif [ "$#" -eq 1 ]; then
    Q_TO_GRADE="Q$1"
    if [ ! -d "$TESTCASES_DIR/$Q_TO_GRADE" ]; then
        print_error "Testcase directory for '$Q_TO_GRADE' not found."
        exit 1
    fi
    print_info "Checking only Question: $Q_TO_GRADE"
    questions=("$Q_TO_GRADE")

else
    echo -e "${YELLOW}Usage: $0 [question_number]${NC}"
    echo "Example: $0 1"
    exit 1
fi

# --- Execution ---
shopt -s nullglob
cpp_files=(*.cpp)
shopt -u nullglob

if [ ${#cpp_files[@]} -gt 0 ]; then
    print_info "Found local .cpp files. Running in student mode..."
    grade_student "./" "${questions[@]}"
else
    for student_dir in */; do
        if [[ "$student_dir" == "$TESTCASES_DIR/" || "$student_dir" == "$ACTUAL_OUTPUT_DIR/" ]]; then continue; fi
        if [ ! -d "${student_dir}" ]; then continue; fi
        shopt -s nullglob
        sub_cpp_files=("${student_dir}"*.cpp)
        shopt -u nullglob
        if [ ${#sub_cpp_files[@]} -eq 0 ]; then continue; fi

        grade_student "$student_dir" "${questions[@]}"
    done
fi

echo -e "${GREEN}${BOLD}======================================================================${NC}"
echo -e "${GREEN}${BOLD} Local checking complete. ${NC}"
echo -e "${GREEN}${BOLD}======================================================================${NC}"
echo -e "${CYAN}Note: Your testcase outputs are saved in the 'actual_output' directory for easy debugging.${NC}"
echo -e "${MAGENTA}Note: Run './submit.sh' to push your code to the server!${NC}\n"
