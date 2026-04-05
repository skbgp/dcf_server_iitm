#!/bin/bash
# submit.sh — uploads your code to the server for grading and shows results

# --- Evaluation Configuration ---
TOTAL_QUESTIONS=1
FULL_MARKS=(50)

# --- ANSI Color Codes & Styles ---
RED='\033[1;31m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
CYAN='\033[1;36m'
MAGENTA='\033[1;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# --- UI Helpers ---
print_header() {
    echo -e "\n${MAGENTA}${BOLD}======================================================================${NC}"
    echo -e "${MAGENTA}${BOLD} $1 ${NC}"
    echo -e "${MAGENTA}${BOLD}======================================================================${NC}\n"
}

print_step()    { echo -e "${BLUE}${BOLD}---> $1${NC}"; }
print_info()    { echo -e "${DIM}[INFO]    $1${NC}"; }
print_success() { echo -e "${GREEN}${BOLD}[SUCCESS] $1${NC}"; }
print_error()   { echo -e "${RED}${BOLD}[ERROR]   $1${NC}"; }
print_warning() { echo -e "${YELLOW}${BOLD}[WARNING] $1${NC}"; }

# --- Source Configuration ---
if [ ! -f "config.sh" ]; then
    print_error "Configuration file 'config.sh' not found."
    echo -e "${DIM}Please create 'config.sh' and set your ROLL_NO.${NC}"
    exit 1
fi
source ./config.sh

if [ -z "$SERVER_URL" ]; then
    SERVER_URL="http://localhost:8000"
    print_warning "SERVER_URL not found in config.sh, defaulting to ${SERVER_URL}"
fi

IP_HEADER_CMD=""
if [ -n "$MOCK_IP" ]; then
    IP_HEADER_CMD="-H X-Lab-Test-IP:$MOCK_IP"
    print_info "Mocking IP: ${MOCK_IP}"
fi

# --- Pure-Shell JSON Helpers (no python3 needed) ---
# Extract a simple value from flat JSON by key name.
# Usage: json_val "$json_string" "key"
# Handles: strings, numbers, null. Does NOT handle nested objects.
json_val() {
    local json="$1" key="$2"
    # Try quoted string value first
    local val=$(echo "$json" | grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 | sed "s/\"${key}\"[[:space:]]*:[[:space:]]*\"//;s/\"$//")
    if [ -n "$val" ]; then
        echo "$val"
        return
    fi
    # Try unquoted value (number, null, bool)
    val=$(echo "$json" | grep -o "\"${key}\"[[:space:]]*:[[:space:]]*[^,}\"]*" | head -1 | sed "s/\"${key}\"[[:space:]]*:[[:space:]]*//;s/[[:space:]]*$//")
    if [ "$val" = "null" ]; then
        echo ""
    else
        echo "$val"
    fi
}

# Extract the nested "result" object as a raw JSON substring.
# "result" is the last field in the response, so we extract everything
# after "result": and strip the outermost trailing brace.
json_result_block() {
    local json="$1"
    # Remove everything up to "result": then strip the outer closing }
    local block=$(echo "$json" | sed 's/.*"result"[[:space:]]*:[[:space:]]*//' | sed 's/}$//')
    if [ -n "$block" ]; then
        echo "$block"
        return
    fi
    # Fallback: try string-escaped JSON
    block=$(echo "$json" | sed -n 's/.*"result"[[:space:]]*:[[:space:]]*"\({.*}\)".*/\1/p' | sed 's/\\"/"/g')
    echo "$block"
}

# List test result keys from the "results" block inside the result JSON.
# Input: the result JSON block. Output: test names, one per line.
json_test_keys() {
    local json="$1"
    # Extract the "results" sub-object
    local results_block=$(echo "$json" | sed -n 's/.*"results"[[:space:]]*:[[:space:]]*{\([^}]*\)}.*/\1/p')
    # Extract keys
    echo "$results_block" | grep -o '"[^"]*"[[:space:]]*:' | sed 's/"//g;s/[[:space:]]*:$//' | sort
}

# Get a test verdict from the results block.
# Usage: json_test_verdict "$result_json" "test_name"
json_test_verdict() {
    local json="$1" key="$2"
    echo "$json" | grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 | sed "s/\"${key}\"[[:space:]]*:[[:space:]]*\"//;s/\"$//"
}

# Find student rank from leaderboard JSON array.
# Usage: json_rank "$leaderboard_json" "ROLL_NO"
json_rank() {
    local json="$1" roll="$2"
    local roll_upper=$(echo "$roll" | tr '[:lower:]' '[:upper:]')
    # Find the rank field near the matching roll
    echo "$json" | tr '{}' '\n' | grep -i "\"roll\"[[:space:]]*:[[:space:]]*\"${roll_upper}\"" | grep -o '"rank"[[:space:]]*:[[:space:]]*[0-9]*' | head -1 | grep -o '[0-9]*'
}

# --- Pre-flight Checks ---
if [ -z "$ROLL_NO" ] || [ "$ROLL_NO" == "YOUR_ROLL_NUMBER" ]; then
    print_error "ROLL_NO is not set properly in 'config.sh'."
    exit 1
fi

if ! command -v curl &> /dev/null; then
    print_error "curl is required but not found. Please install curl."
    exit 1
fi

# --- Main Logic ---
process_question() {
    local Q_NO_NUM=$1
    local Q_NO="Q${Q_NO_NUM}"
    local FILE_PATH="./${ROLL_NO}/${Q_NO}.cpp"
    local QUESTION_FULL_MARKS=${FULL_MARKS[$((Q_NO_NUM - 1))]}

    if [ ! -f "$FILE_PATH" ]; then
        if [ -f "./${Q_NO}.cpp" ]; then
            FILE_PATH="./${Q_NO}.cpp"
        else
            print_error "File not found at './${ROLL_NO}/${Q_NO}.cpp' or './${Q_NO}.cpp'"
            return 1
        fi
    fi

    echo -e "${YELLOW}${BOLD}=> Submitting ${Q_NO} for ${ROLL_NO}...${NC}"
    print_step "Uploading file to server..."

    BASE_URL="${SERVER_URL%/}"
    local SUBMIT_RESPONSE=$(MSYS_NO_PATHCONV=1 curl -s --connect-timeout 5 --max-time 15 -X POST \
      $IP_HEADER_CMD -F "roll=${ROLL_NO}" -F "file=@${FILE_PATH}" "${BASE_URL}/submit/${Q_NO}")

    local TASK_ID=$(json_val "$SUBMIT_RESPONSE" "taskid")

    if [ -z "$TASK_ID" ] || [ "$TASK_ID" == "null" ]; then
        local SERVER_ERR=$(json_val "$SUBMIT_RESPONSE" "response")
        if [ -n "$SERVER_ERR" ]; then 
            print_error "$SERVER_ERR"
            if [[ "$SERVER_ERR" == *"Violation"* ]] || [[ "$SERVER_ERR" == *"not registered"* ]] || [[ "$SERVER_ERR" == *"outside the authorized"* ]]; then
                echo -e "${RED}${BOLD}Aborting further submissions due to network restriction.${NC}"
                exit 1
            fi
        else 
            print_error "Failed to connect to ${BASE_URL}"
        fi
        return 1
    fi

    print_success "Submission accepted!"
    print_step "Awaiting server evaluation"
    
    echo -en "${DIM}    Polling server "
    local STATUS_RESPONSE
    local TASK_STATUS
    while true; do
        STATUS_RESPONSE=$(MSYS_NO_PATHCONV=1 curl -s --connect-timeout 5 --max-time 10 $IP_HEADER_CMD "${BASE_URL}/task-status/${TASK_ID}")
        TASK_STATUS=$(json_val "$STATUS_RESPONSE" "status")

        if [ "$TASK_STATUS" != "PENDING" ]; then
            echo -e "${NC}"
            break
        fi
        echo -en "."
        sleep 2
    done

    if [ "$TASK_STATUS" != "SUCCESS" ]; then
        local ERROR_MSG=$(json_val "$STATUS_RESPONSE" "result")
        [ -z "$ERROR_MSG" ] && ERROR_MSG="Server error"
        print_error "Evaluation failed: ${ERROR_MSG}"
        return 1
    fi

    local FINAL_RESULT=$(json_result_block "$STATUS_RESPONSE")
    local APP_STATUS=$(json_val "$FINAL_RESULT" "status")

    if [[ "$APP_STATUS" == *"Compilation Error"* ]]; then
        print_error "Compilation Error Occurred on Server:"
        echo -e "${DIM}--------------------------------------------------${NC}"
        json_val "$FINAL_RESULT" "details"
        echo -e "${DIM}--------------------------------------------------${NC}"

    elif [ "$APP_STATUS" == "Finished" ]; then
        print_step "Server Test Results:"
        echo ""
        
        json_test_keys "$FINAL_RESULT" | while IFS= read -r test_name; do
            [ -z "$test_name" ] && continue
            local VERDICT=$(json_test_verdict "$FINAL_RESULT" "$test_name")
            local FORMATTED_VERDICT=""
            case "$VERDICT" in
                "Passed") FORMATTED_VERDICT="${GREEN}${BOLD}Passed${NC}" ;;
                "Time Limit Exceeded") FORMATTED_VERDICT="${YELLOW}${BOLD}Time Limit Exceeded${NC}" ;;
                "Runtime Error") FORMATTED_VERDICT="${RED}${BOLD}Runtime Error${NC}" ;;
                "Wrong Answer") FORMATTED_VERDICT="${RED}${BOLD}Wrong Answer${NC}" ;;
                *) FORMATTED_VERDICT="${BLUE}${BOLD}${VERDICT}${NC}" ;;
            esac
            # Matches check.sh print alignment
            printf "    %-20s [%b]\n" "${test_name}" "$FORMATTED_VERDICT"
        done

        local PASSED_COUNT=$(json_val "$FINAL_RESULT" "passed")
        local FAILED_COUNT=$(json_val "$FINAL_RESULT" "failed")
        local OBTAINED_MARKS=$(json_val "$FINAL_RESULT" "marks")
        
        local LEADERBOARD_RESPONSE=$(MSYS_NO_PATHCONV=1 curl -s --connect-timeout 5 --max-time 10 $IP_HEADER_CMD "${BASE_URL}/api/leaderboard/${Q_NO}?force_refresh=true")
        local STUDENT_RANK=$(json_rank "$LEADERBOARD_RESPONSE" "${ROLL_NO}")
        if [ -z "$STUDENT_RANK" ] || [ "$STUDENT_RANK" == "null" ]; then STUDENT_RANK="-"; fi

        # Consistent Summary Box
        echo -e "\n    ┌────────────────────────────────────────┐"
        printf  "    │ ${BOLD}%-38s${NC} │\n" "SERVER SUMMARY: ${Q_NO}"
        echo -e "    ├────────────────────────────────────────┤"
        printf  "    │ %-20s ${GREEN}%-17s${NC} │\n" "Passed Tests:" "${PASSED_COUNT}"
        printf  "    │ %-20s ${RED}%-17s${NC} │\n" "Failed Tests:" "${FAILED_COUNT}"
        printf  "    │ %-20s ${CYAN}%-17s${NC} │\n" "Marks:" "${OBTAINED_MARKS} / ${QUESTION_FULL_MARKS}"
        printf  "    │ %-20s ${YELLOW}%-17s${NC} │\n" "Leaderboard Rank:" "#${STUDENT_RANK}"
        echo -e "    └────────────────────────────────────────┘\n"

    else
        print_error "Unexpected Server Status: ${APP_STATUS}"
    fi
}

# -> CHANGED BANNER TEXT HERE <-
print_header "Server Evaluation & Submission"

if [ "$#" -eq 0 ]; then
    print_info "Processing all ${TOTAL_QUESTIONS} questions..."
    for (( i=1; i<=TOTAL_QUESTIONS; i++ )); do
        process_question "$i"
        if [ "$i" -ne "$TOTAL_QUESTIONS" ]; then
            sleep 5
        fi
    done
elif [ "$#" -eq 1 ]; then
    process_question "$1"
else
    echo -e "${YELLOW}Usage: $0 [question_number]${NC}"
    echo "Example: $0 1"
    exit 1
fi

# -> CHANGED OUTRO TEXT HERE TO MATCH <-
echo -e "${MAGENTA}${BOLD}======================================================================${NC}"
echo -e "${MAGENTA}${BOLD} Evaluation & submission complete. ${NC}"
echo -e "${MAGENTA}${BOLD}======================================================================${NC}"
echo -e "${CYAN}Note: Need to debug? Run './check.sh' to test your code locally!${NC}\n"