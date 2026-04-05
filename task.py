# task.py - celery worker that compiles and grades student submissions
# Author: Shubham (CS25M046) — https://github.com/skbgp/dcf_server_iitm
# Copyright (c) 2026 Shubham (CS25M046). All rights reserved. See LICENSE.
# pipeline: save .cpp -> compile (in bwrap sandbox on linux) -> run against
# test cases -> calculate marks -> update grades.csv

import os
import sys
import signal
import shutil
import time
import glob
import subprocess
import re
import resource
from concurrent.futures import ThreadPoolExecutor, as_completed

# Resource limits for student processes.
# These prevent a single submission from taking down the server.
MAX_MEMORY_BYTES = 256 * 1024 * 1024     # 256 MB virtual memory
MAX_CPU_SECONDS  = 60                     # 60s total CPU time (hard kill)
MAX_FILE_BYTES   = 64 * 1024 * 1024       # 64 MB max file write
MAX_OUTPUT_BYTES = 1 * 1024 * 1024        # 1 MB max stdout capture

def _set_resource_limits():
    """Called as preexec_fn inside subprocess — sets hard resource limits.
    
    Runs in the child process before exec(), so these limits apply to
    the student's compiled binary (or the bwrap wrapper around it).
    Only works on Linux/macOS, which is fine since those are our targets.
    """
    try:
        # Memory limit — kills process with ENOMEM if exceeded
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    except (ValueError, resource.error):
        pass  # some systems don't support RLIMIT_AS
    try:
        # CPU time limit — sends SIGKILL when exceeded
        resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
    except (ValueError, resource.error):
        pass
    try:
        # File size limit — prevents writing huge files to disk
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
    except (ValueError, resource.error):
        pass
    # Start a new process group so we can kill all children at once on timeout
    os.setpgrp()

from celery import Celery

# Check if bubblewrap is installed. It's a Linux-only tool, so this will
# be False on macOS. When it's missing, we skip sandboxing (fine for dev,
# but NEVER run an untrusted student's code without a sandbox in production).
HAS_BWRAP = shutil.which("bwrap") is not None
if not HAS_BWRAP:
    print("WARNING: bwrap not found, running without sandbox (dev mode)")

# course config — marks and timeouts per question
# re-read from course.conf on every submission so you can change mid-exam
_last_conf_mtime = 0          # tracks when we last read the file
timeouter_list = [2, 2]       # timeout in seconds per question (default 2s each)
fm_list = [50, 50]            # full marks per question (default 50 each)

def _load_course_conf():
    """Load marks and timeout values from course.conf.
    
    Only re-reads the file if it's been modified since last check.
    Format of course.conf is simple key=value:
      fm_list=50,50,100
      timeouts=2,3,5
    """
    global timeouter_list, fm_list, _last_conf_mtime
    conf_path = ".active_lab/course.conf"
    if not os.path.exists(conf_path):
        return
    try:
        current_mtime = os.path.getmtime(conf_path)
        if current_mtime == _last_conf_mtime:
            return  # file hasn't changed since last load
            
        with open(conf_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("fm_list="):
                    fm_list = [float(x) for x in line.split("=", 1)[1].split(",")]
                elif line.startswith("timeouts="):
                    timeouter_list = [float(x) for x in line.split("=", 1)[1].split(",")]
        
        _last_conf_mtime = current_mtime
        print(f"Loaded course.conf: marks={fm_list}, timeouts={timeouter_list}")
    except Exception as e:
        print(f"Warning: error reading course.conf: {e}. Using defaults.")

_load_course_conf()  # load once at worker startup

# Celery app — uses Redis as both the message broker and result backend.
# Redis runs on localhost:6379, started automatically by start.sh.
capp = Celery(
    'task',
    broker="redis://localhost:6379",
    backend="redis://localhost:6379"
    )


# --- test runner ---
# runs a single test case in its own sandbox directory

def _run_single_test(i, testcase, executable_path, output_dir, sub_dir, timeouter):
    """Run one test case and return the verdict (Passed/Wrong Answer/TLE/etc.).
    
    Creates an isolated sandbox directory, copies the binary in, runs it
    with the test input piped to stdin, and compares stdout to expected output.
    Cleans up the sandbox afterwards regardless of the outcome.
    """
    i_str = f"{i:02d}"     # zero-padded test number (01, 02, ... 99)
    # Use the filename (input01) instead of generic "Test 01" label
    test_name = os.path.basename(testcase).replace(".txt", "")
    test_filename = os.path.basename(testcase)
    expected_filename = test_filename.replace("input", "output")  # input01.txt → output01.txt
    expected_path = os.path.join(output_dir, expected_filename)

    result = {
        "test_name": test_name,
        "verdict": None,
        "actual_output": "",
        "expected_output": "",
        "log_lines": [],
        "error_abort": False,    # if True, skip remaining tests (fatal error)
    }

    if not os.path.exists(expected_path):
        result["log_lines"].append(f"ERROR: Corresponding expected output file not found at {expected_filename}\n")
        result["verdict"] = "Configuration Error"
        return result

    result["log_lines"].append(f"\n--- Running {test_name} with {test_filename} ---\n")

    # Create an isolated sandbox for this test. Each test gets its own directory
    # so tests can't interfere with each other (important when running in parallel).
    sandbox_dir = os.path.join(sub_dir, f"sandbox_{i_str}")
    if os.path.exists(sandbox_dir):
        shutil.rmtree(sandbox_dir)
    os.makedirs(sandbox_dir, exist_ok=True)

    # We no longer copy the binary to avoid MacOS Gatekeeper scanning 50 "new" files simultaneously,
    # which causes random 2+ second delays and Time Limit Exceeded errors on empty files.
    # Instead, we just execute the original compiled binary. For Linux bwrap, we bind-mount it.

    # Build the run command — either with bwrap sandbox (Linux) or direct exec (macOS)
    if HAS_BWRAP:
        # bubblewrap command: creates an isolated filesystem namespace.
        # --ro-bind: mount system libs as read-only (student can't modify them)
        # --bind: the sandbox dir is the only writable directory
        # --unshare-all: no network, no PIDs visible, no IPC — full isolation
        # --die-with-parent: if the worker dies, the sandboxed process dies too
        run_command = [
            "bwrap",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--symlink", "/usr/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--tmpfs", "/var",
            "--ro-bind", os.path.abspath(executable_path), os.path.abspath(executable_path),
            "--bind", os.path.abspath(sandbox_dir), os.path.abspath(sandbox_dir),
            "--unshare-all",
            "--die-with-parent",
            os.path.abspath(executable_path)
        ]
    else:
        # macOS fallback: no sandbox, just run the binary directly.
        # Fine for local testing but NOT secure for real exams.
        run_command = [os.path.abspath(executable_path)]

    proc = None
    try:
        # Pipe the test input into the student's program via stdin.
        # We use Popen (not run) so we can kill the entire process group on
        # timeout — subprocess.run only kills the parent, leaving children alive.
        with open(testcase, "r") as input_f:
            proc = subprocess.Popen(
                run_command,
                stdin=input_f,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=sandbox_dir,
                preexec_fn=_set_resource_limits
            )

            try:
                raw_out, raw_err = proc.communicate(timeout=timeouter)
            except subprocess.TimeoutExpired:
                # Kill the entire process group (parent + any children it spawned)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()  # fallback: kill just the parent
                proc.wait()
                result["verdict"] = "Time Limit Exceeded"
                result["log_lines"].append("VERDICT: Time Limit Exceeded\n")
                return result

        # Cap captured output to prevent memory bombs from students
        # printing in infinite loops
        out_str = raw_out[:MAX_OUTPUT_BYTES].decode('utf-8', errors='replace') if raw_out else ""
        err_str = raw_err[:MAX_OUTPUT_BYTES].decode('utf-8', errors='replace') if raw_err else ""

        if proc.returncode != 0:
            # Non-zero exit = crash (segfault, failed assertion, etc.)
            result["verdict"] = "Runtime Error"
            result["log_lines"].append(f"VERDICT: Runtime Error (Return Code: {proc.returncode})\nStderr:\n{err_str}\n")
        else:
            # Program ran successfully — now compare output to expected
            actual_output_file = os.path.join(sub_dir, f"actual_output_{i_str}.txt")
            with open(actual_output_file, "w", encoding='utf-8') as f:
                f.write(out_str)

            # We compare outputs as whitespace-split tokens, not raw strings.
            # This means trailing newlines and extra spaces don't cause failures.
            actual_output = out_str.split()
            result["actual_output"] = out_str
            with open(expected_path, 'r') as f:
                exp_content = f.read()
                expected_output = exp_content.split()
                result["expected_output"] = exp_content

            if actual_output == expected_output:
                result["verdict"] = "Passed"
                result["log_lines"].append(f"VERDICT: Passed\n")
            else:
                result["verdict"] = "Wrong Answer"
                result["log_lines"].append(f"VERDICT: Wrong Answer\n")

    except FileNotFoundError:
        result["log_lines"].append("ERROR: Executable not found or could not be launched.\n")
        result["verdict"] = "Configuration Error"
        result["error_abort"] = True  # stop all remaining tests
    finally:
        # Make sure the process is dead before cleaning up
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait()
        # Always clean up the sandbox directory, even on crashes
        if os.path.exists(sandbox_dir):
            shutil.rmtree(sandbox_dir, ignore_errors=True)

    return result


# --- main grading pipeline (called by celery) ---

@capp.task(name="handle-sub")
def handle_submission(qno: str, roll: str, filename: str, content: str):
    """Grade a student's submission. Called asynchronously by Celery.
    
    Args:
        qno: Question number (e.g., "Q1")
        roll: Student roll number (e.g., "CS24B001")
        filename: Original filename from the upload (e.g., "Q1_CS24B001.cpp")
        content: The actual source code as a string
    
    Returns:
        Dict with status, test results, marks, etc.
    """
    # Reload config in case the admin changed marks/timeouts since last submission
    _load_course_conf()
    
    qno_upper = qno.upper()
    roll_upper = roll.upper()

    # Extract the question number from various formats: "Q1", "q1", "Question1" → 1
    match = re.search(r'([0-9]+)$', qno_upper)
    if not match:
        return {"status": "Configuration Error", "message": f"Invalid Question Number format: {qno}"}
    qno_int = int(match.group(1))

    # Look up marks and timeout for this question from course.conf
    if qno_int <= len(fm_list):
        fm = fm_list[qno_int - 1]
    else:
        fm = fm_list[-1] if fm_list else 50  # fallback to last value or default
        
    if qno_int <= len(timeouter_list):
        timeouter = timeouter_list[qno_int - 1]
    else:
        timeouter = timeouter_list[-1] if timeouter_list else 2

    logs = [f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Processing submission for Roll: {roll_upper}, Q-No: {qno_upper}\n"]

    # Directory structure:
    #   .active_lab/submissions/Q1/CS24B001/20260320-113000/
    #                          ^^^ question  ^^^ student   ^^^ timestamp
    q_dir = os.path.join(".active_lab", "submissions", qno_upper)
    std_dir = os.path.join(q_dir, roll_upper)

    os.makedirs(std_dir, exist_ok=True) 

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    
    # Each submission gets its own timestamped subdirectory so we keep a full history
    sub_dir = os.path.join(std_dir, timestamp)
    os.makedirs(sub_dir, exist_ok=True)

    # Sanitize the filename to prevent path traversal, then add a timestamp to avoid collisions
    safe_filename = os.path.basename(filename)
    base, ext = os.path.splitext(safe_filename)
    save_filename = f"{base}_{timestamp}{ext}"
    save_file = os.path.join(sub_dir, save_filename)
    executable_path = os.path.join(sub_dir, "submission.out")
    
    log_path = os.path.join(sub_dir, f"result_{timestamp}.txt")

    # Step 1: Save the source code to disk
    try:
        with open(save_file, "w", encoding='utf-8') as f: f.write(content)
        logs.append(f"SUCCESS: Source file saved to {save_file}\n")
    except Exception as e:
        logs.append(f"ERROR: Failed to save source file. Reason: {e}\n")
        with open(log_path, "w") as log_file: log_file.writelines(logs)
        return {"status": "Setup Error", "message": "Could not save file."}
    
    # marks.txt stays at the student level (not per-submission) so we can track
    # all their attempts over time. Each line is: timestamp, marks
    marks_log = os.path.join(std_dir, "marks.txt")
    
    # Step 2: Compile the code
    # On Linux, compilation happens inside bubblewrap too — this prevents the
    # student from using #include to read testcase files at compile time.
    logs.append("INFO: Compiling C++ code with -O2 optimization...\n")
    if HAS_BWRAP:
        # Sandboxed compilation: read-only access to system libs, write access only to sub_dir
        compile_command = [
            "bwrap",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--symlink", "/usr/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--ro-bind", os.path.abspath(save_file), os.path.abspath(save_file),
            "--bind", os.path.abspath(sub_dir), os.path.abspath(sub_dir),
            "--unshare-net",        # no network access during compilation
            "--die-with-parent",
            "g++", os.path.abspath(save_file), "-std=c++17", "-O2", "-pipe", "-o", os.path.abspath(executable_path)
        ]
    else:
        # macOS fallback: compile without sandbox
        compile_command = [
            "g++", os.path.abspath(save_file), "-std=c++17", "-O2", "-pipe", "-o", os.path.abspath(executable_path)
        ]
    compile_proc = subprocess.run(compile_command, capture_output=True, text=True, timeout=30)
    if compile_proc.returncode != 0:
        # Compilation failed — log the error and record 0 marks
        logs.append(f"ERROR: Compilation failed.\nCompiler Output:\n{compile_proc.stderr}\n")
        with open(log_path, "w") as log_file: log_file.writelines(logs)
        line = f"{timestamp}, 0\n"
        with open(marks_log, "a") as f:
            f.write(line)
        return {"status": "Compilation Error", "results": {}, "details": compile_proc.stderr}
    logs.append("SUCCESS: Compilation successful.\n")

    os.chmod(executable_path, 0o755)

    # Step 3: Run all test cases in parallel
    # We use ThreadPoolExecutor to run tests concurrently for speed.
    # With 30 test cases and 2s timeout each, sequential would take 60s worst case.
    # Parallel gets it done in ~2-4s.
    test_dir = os.path.join(".active_lab", "testcases", qno_upper, "input")
    output_dir = os.path.join(".active_lab", "testcases", qno_upper, "output")

    test_files = sorted(glob.glob(os.path.join(test_dir, "input*.txt")))
    if not test_files:
        logs.append(f"ERROR: No test cases found in {test_dir}\n")
        with open(log_path, "w") as log_file: log_file.writelines(logs)
        return {"status": "Configuration Error", "results": {}, "message": "No test cases found."}
    
    total = len(test_files)
    test_results = {}

    # Run up to 8 tests at a time (or fewer if there aren't that many)
    with ThreadPoolExecutor(max_workers=min(total, 8)) as executor:
        futures = {
            executor.submit(_run_single_test, i, tc, executable_path, output_dir, sub_dir, timeouter): i
            for i, tc in enumerate(test_files, 1)
        }
        for future in as_completed(futures):
            res = future.result()
            test_results[futures[future]] = res

    # Step 4: Collect and tally results
    results = {}
    actual_outputs = {}
    expected_outputs = {}
    passed = 0

    for i in sorted(test_results.keys()):
        res = test_results[i]
        test_name = res["test_name"]
        results[test_name] = res["verdict"]
        actual_outputs[test_name] = res["actual_output"]
        expected_outputs[test_name] = res["expected_output"]
        logs.extend(res["log_lines"])
        if res["verdict"] == "Passed":
            passed += 1
        if res["error_abort"]:
            # Fatal error (e.g., binary not found) — no point running more tests
            with open(log_path, "w") as log_file: log_file.writelines(logs)
            return {"status": "Configuration Error", "results": {}, "message": "Executable not found."}

    # Write the final result log
    logs.append("\n--- FINAL RESULTS ---\n")
    for test, result in results.items():
        logs.append(f"{test}: {result}\n")

    with open(log_path, "w") as log_file:
        log_file.writelines(logs)

    # Step 5: Calculate marks proportionally (passed/total * full_marks)
    failed = total - passed
    marks = round((passed / total) * fm, 2)

    # Append to marks.txt so we have a history of all submission scores
    line = f"{timestamp}, {marks}\n"
    with open(marks_log, "a") as f:
        f.write(line)

    # Update the master grades.csv with the BEST mark for this question
    _update_grades_csv(roll_upper, qno_upper, marks)

    return {"status": "Finished", "results": results, "passed": passed, "failed": failed, "marks": marks, "full": fm}


# --- grades.csv management ---
# keeps track of each student's best score per question

def _update_grades_csv(roll: str, qno: str, new_marks: float):
    """Update grades.csv with the student's best marks for a question.
    
    Only replaces the existing score if the new submission scored higher.
    Also maintains a Total column that sums all question scores.
    Uses file locking (fcntl) to prevent corruption from concurrent workers.
    """
    import csv as csv_mod
    import fcntl
    grades_file = ".active_lab/grades.csv"
    lock_file = ".active_lab/grades.csv.lock"
    lock_fd = open(lock_file, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)  # wait for exclusive lock

        # Figure out header columns: one per question (Q1, Q2, etc.)
        global fm_list
        num_questions = len(fm_list)
        all_questions = [f"Q{i+1}" for i in range(num_questions)]
        if qno not in all_questions:
            all_questions.append(qno)

        # Load the full student roster so we can fill in "Absent" for
        # students who never submitted anything
        all_students = []
        students_file = ".active_lab/students.txt"
        if os.path.exists(students_file):
            with open(students_file, "r") as sf:
                for line in sf:
                    r = line.strip().upper()
                    if r:
                        all_students.append(r)
    
        # Distinguish "registered but didn't submit" (0.0) from
        # "didn't even register" (Absent)
        registered_students = set()
        registrations_file = ".active_lab/registrations.csv"
        if os.path.exists(registrations_file):
            with open(registrations_file, "r", newline="", encoding="utf-8") as rf:
                rdr = csv_mod.DictReader(rf)
                for row in rdr:
                    if row.get("roll_no"):
                        registered_students.add(row["roll_no"].upper())

        # Read the existing grades.csv into memory. We'll modify it and write it back.
        from typing import Dict, Set
        grades: Dict[str, Dict[str, str]] = {}
        if os.path.exists(grades_file):
            with open(grades_file, "r", newline="", encoding="utf-8") as f:
                reader = csv_mod.DictReader(f)
                fields = reader.fieldnames if reader.fieldnames else []
                # Merge any extra question columns we didn't know about
                for field in fields:
                    if field.startswith("Q") and field[1:].isdigit() and field not in all_questions:
                        all_questions.append(field)
                for row in reader:
                    # Ignore corrupted columns, only keep strictly valid question keys
                    grades[str(row["roll"])] = {str(k): str(v) for k, v in row.items() if k in all_questions}
    
        # Fill in any students/questions that don't have rows yet.
        # Registered students get "0.0" (they showed up), others get "Absent".
        for s in all_students:
            if s not in grades:
                grades[s] = {}
            for q in all_questions:
                if q not in grades[s] or grades[s][q] == "":
                    if s in registered_students:
                        grades[s][q] = "0.0"
                    else:
                        grades[s][q] = "Absent"
                
        # make sure current submitter has a row
        if roll not in grades:
            is_registered = roll in registered_students
            default_val = "0.0" if is_registered else "Absent"
            grades[roll] = {q: default_val for q in all_questions}

        # flip any 'Absent' entries to '0.0' for this student
        for q in all_questions:
            if grades.get(roll, {}).get(q) in ("Absent", ""):
                grades[roll][q] = "0.0"

        def parse_mark(val):
            if val == "Absent" or val == "":
                return -1.0
            try:
                return float(val)
            except ValueError:
                return -1.0

        # Only update if this submission scored HIGHER than the previous best.
        # This means students' grades never go down — the best attempt is kept.
        current_best = parse_mark(grades[roll].get(qno, "Absent"))
        if float(new_marks) > current_best:
            grades[roll][qno] = str(float(new_marks))
            # Touch the question directory to bust the leaderboard cache
            # (main.py checks directory mtime to know when to refresh)
            try:
                q_dir = os.path.join(".active_lab", "submissions", qno)
                os.utime(q_dir, None)
            except OSError:
                pass
    
        # Rebuild the CSV: sort questions numerically (Q1, Q2, ...), add Total column
        def q_sort(q):
            if q == "Total": return 9999
            return int(q[1:]) if q.startswith("Q") and q[1:].isdigit() else 999
        
        sorted_questions = sorted([q for q in all_questions if q != "Total"], key=q_sort)
        headers = ["roll"] + sorted_questions
        if len(sorted_questions) > 1:
            headers.append("Total")  # only add Total if there's more than 1 question
    
        with open(grades_file, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            for r in sorted(grades.keys()):
                row = {"roll": r}
                total_score = 0.0
                for q in sorted_questions:
                    val = grades[r].get(q, "Absent")
                    row[q] = val
                    # add to total
                    if val and val != "Absent":
                        try:
                            total_score += float(val)
                        except ValueError:
                            pass
                        
                if len(sorted_questions) > 1:
                    row["Total"] = round(total_score, 2)
                
                writer.writerow(row)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
