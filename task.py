# task.py - celery worker that compiles and grades student submissions
# Author: Shubham (CS25M046) — https://github.com/skbgp/dcf_server_iitm
# Copyright (c) 2026 Shubham (CS25M046). All rights reserved. See LICENSE.
# pipeline: save .cpp -> compile (in bwrap sandbox on linux) -> run against
# test cases -> calculate marks -> update grades.csv

import json
import logging
import math
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from celery import Celery
from celery.signals import task_postrun
from redis.exceptions import RedisError

from file_state import locked, replace_text
from grading_queue import OWNER_TTL, release
from sandbox import DEVELOPMENT_MODE, OutputLimitExceeded, bubblewrap, require_sandbox, run_bounded

MAX_TEST_CASES = 256
MAX_SOURCE_BYTES = 256 * 1024
MAX_CASE_SECONDS = 30
logger = logging.getLogger(__name__)

def load_course_conf(lab):
    values = {}
    with (lab / "course.conf").open(encoding="utf-8") as config:
        for line in config:
            name, _, value = line.strip().partition("=")
            if name in {"fm_list", "timeouts"}:
                values[name] = [float(item) for item in value.split(",")]
    marks, timeouts = values["fm_list"], values["timeouts"]
    if (len(marks) != len(timeouts) or not marks or
            any(not math.isfinite(mark) or mark < 0 for mark in marks) or
            any(not 0 < seconds <= MAX_CASE_SECONDS for seconds in timeouts)):
        raise ValueError("invalid marks or timeouts")
    return marks, timeouts


redis_url = os.environ.get("DCF_REDIS_URL", "redis://127.0.0.1:6379/0")
capp = Celery("task", broker=redis_url, backend=redis_url)
capp.conf.update(
    result_expires=OWNER_TTL, task_acks_late=True,
    task_reject_on_worker_lost=True, worker_prefetch_multiplier=1, task_time_limit=1200,
)


@task_postrun.connect
def release_admission(sender=None, task_id=None, kwargs=None, state=None, **_):
    if getattr(sender, "name", None) == "handle-sub" and state in ("SUCCESS", "FAILURE") and kwargs and kwargs.get("lab"):
        try:
            release(capp.backend.client, task_id, kwargs["lab"], kwargs["roll"])
        except RedisError:
            logger.exception("Could not release grading slot for %s", task_id)


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
    expected_filename = "output" + test_filename.removeprefix("input")  # input01.txt → output01.txt
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
        result["error_abort"] = True
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
    if DEVELOPMENT_MODE:
        command = [os.path.abspath(executable_path)]
    else:
        command = bubblewrap() + [
            "--ro-bind", os.path.abspath(executable_path), "/submission",
            "--bind", os.path.abspath(sandbox_dir), "/work",
            "--chdir", "/work", "/submission",
        ]

    try:
        with open(testcase, "rb") as input_file:
            run = run_bounded(command, timeout=timeouter, memory_mib=256,
                              cwd=sandbox_dir, stdin=input_file)
    except subprocess.TimeoutExpired:
        result["verdict"] = "Time Limit Exceeded"
    except OutputLimitExceeded:
        result["verdict"] = "Output Limit Exceeded"
    except OSError as exc:
        result["verdict"] = "Configuration Error"
        result["error_abort"] = True
        result["log_lines"].append(str(exc) + "\n")
    else:
        out_str = run.stdout.decode("utf-8", errors="replace")
        err_str = run.stderr.decode("utf-8", errors="replace")
        if run.returncode and err_str.startswith("bwrap:"):
            result["verdict"] = "Configuration Error"
            result["error_abort"] = True
            result["log_lines"].append(err_str + "\n")
        elif run.returncode:
            result["verdict"] = "Runtime Error"
            result["log_lines"].append(f"Return code: {run.returncode}\n{err_str}\n")
        else:
            actual_output_file = os.path.join(sub_dir, f"actual_output_{i_str}.txt")
            with open(actual_output_file, "w", encoding="utf-8") as f:
                f.write(out_str)
            with open(expected_path, encoding="utf-8") as expected_file:
                expected = expected_file.read()
            result["verdict"] = "Passed" if out_str.split() == expected.split() else "Wrong Answer"
    finally:
        shutil.rmtree(sandbox_dir, ignore_errors=True)

    result["log_lines"].append(f"VERDICT: {result['verdict']}\n")
    return result


# --- main grading pipeline (called by celery) ---

@capp.task(bind=True, name="handle-sub", autoretry_for=(OSError,), retry_backoff=True, max_retries=3)
def handle_submission(self, qno, roll, filename, content, lab=None, roster=None, attempt=None):
    lab = Path(lab or ".active_lab").resolve()
    roster = Path(roster or ".active_lab/students.txt").resolve()
    qno, roll = qno.upper(), roll.upper()
    if not re.fullmatch(r"Q[1-9][0-9]*", qno) or not re.fullmatch(r"[A-Z0-9_]{1,40}", roll):
        return {"status": "Configuration Error", "message": "Invalid question or roll number"}
    if Path(filename).suffix.lower() != ".cpp" or len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        return {"status": "Configuration Error", "message": "Invalid C++ source"}
    try:
        require_sandbox()
        marks, timeouts = load_course_conf(lab)
        index = int(qno[1:]) - 1
        full_mark, timeout = marks[index], timeouts[index]
    except (RuntimeError, OSError, ValueError, KeyError, IndexError) as exc:
        logger.error("Grader configuration: %s", exc)
        return {"status": "Configuration Error", "message": "Grader configuration unavailable"}

    inputs = sorted((lab / "testcases" / qno / "input").glob("input*.txt"))
    outputs = lab / "testcases" / qno / "output"
    if not 0 < len(inputs) <= MAX_TEST_CASES or any(
        not (outputs / ("output" + item.name.removeprefix("input"))).is_file()
        for item in inputs
    ):
        return {"status": "Configuration Error", "message": "Missing or invalid test cases"}

    attempt = attempt or self.request.id or uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", attempt):
        return {"status": "Configuration Error", "message": "Invalid attempt ID"}
    question = lab / "submissions" / roll / qno
    directory = question / attempt
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / "result.json"
    with locked(directory / ".lock"):
        if receipt.exists():
            result = json.loads(receipt.read_text(encoding="utf-8"))
        else:
            result = grade_attempt(directory, qno, content, inputs, outputs, timeout, full_mark)
            with replace_text(receipt) as file:
                json.dump(result, file)
        if "marks" in result:
            update_attempts(question, attempt, result["marks"])
            _update_grades_csv(roll, qno, result["marks"], lab, roster, len(marks))
        return result


def grade_attempt(directory, qno, content, inputs, outputs, timeout, full_mark):
    source = directory / f"{qno}_{directory.name}.cpp"
    source.write_text(content, encoding="utf-8")
    build = directory / "build"
    build.mkdir(exist_ok=True)
    executable = build / "submission.out"
    executable.unlink(missing_ok=True)
    if DEVELOPMENT_MODE:
        command = ["g++", str(source), "-std=c++17", "-O2", "-o", str(executable)]
    else:
        command = bubblewrap() + [
            "--ro-bind", str(source), "/source.cpp",
            "--bind", str(build), "/work", "--chdir", "/work",
            "g++", "/source.cpp", "-std=c++17", "-O2", "-o", "/work/submission.out",
        ]
    try:
        compiled = run_bounded(command, timeout=30, memory_mib=512, cwd=build)
    except subprocess.TimeoutExpired:
        return {"status": "Compilation Error", "details": "Compiler timed out", "results": {}, "marks": 0}
    except OutputLimitExceeded:
        return {"status": "Compilation Error", "details": "Compiler output limit exceeded", "results": {}, "marks": 0}
    except OSError:
        return {"status": "Configuration Error", "message": "Compiler could not start"}
    if compiled.returncode:
        message = compiled.stderr.decode("utf-8", errors="replace")
        if message.startswith("bwrap:"):
            logger.error("Compile sandbox failed: %s", message)
            return {"status": "Configuration Error", "message": "Compiler sandbox could not start"}
        return {"status": "Compilation Error", "details": message, "results": {}, "marks": 0}

    with ThreadPoolExecutor(max_workers=min(len(inputs), 8)) as executor:
        futures = [executor.submit(_run_single_test, index, str(testcase), str(executable),
                                   str(outputs), str(directory), timeout)
                   for index, testcase in enumerate(inputs, 1)]
        verdicts = [future.result() for future in futures]
    with replace_text(directory / f"result_{directory.name}.txt") as log:
        for verdict in verdicts:
            log.writelines(verdict["log_lines"])
    if any(verdict["error_abort"] for verdict in verdicts):
        return {"status": "Configuration Error", "message": "One or more tests could not run"}
    results = {verdict["test_name"]: verdict["verdict"] for verdict in verdicts}
    passed = sum(verdict == "Passed" for verdict in results.values())
    return {"status": "Finished", "results": results, "passed": passed, "failed": len(inputs) - passed,
            "marks": round(passed / len(inputs) * full_mark, 2), "full": full_mark}


def update_attempts(question, attempt, score):
    history = question / "marks.txt"
    with locked(question / ".marks.lock"):
        entries = history.read_text(encoding="utf-8").splitlines() if history.exists() else []
        if any(line.split(",", 1)[0].strip() == attempt for line in entries):
            return
        with replace_text(history) as log:
            log.write("\n".join([*entries, f"{attempt}, {score}"]) + "\n")


# --- grades.csv management ---
# keeps track of each student's best score per question

def _update_grades_csv(roll, qno, new_marks, lab, students_file, num_questions):
    """Update grades.csv with the student's best marks for a question.
    
    Only replaces the existing score if the new submission scored higher.
    Also maintains a Total column that sums all question scores.
    Uses file locking (fcntl) to prevent corruption from concurrent workers.
    """
    import csv as csv_mod
    grades_file = lab / "grades.csv"
    with locked(lab / "grades.csv.lock"):
        all_questions = [f"Q{i+1}" for i in range(num_questions)]
        if qno not in all_questions:
            all_questions.append(qno)

        # Load the full student roster so we can fill in "Absent" for
        # students who never submitted anything
        all_students = []
        if os.path.exists(students_file):
            with open(students_file, "r") as sf:
                for line in sf:
                    r = line.strip().upper()
                    if r:
                        all_students.append(r)
    
        # Distinguish "registered but didn't submit" (0.0) from
        # "didn't even register" (Absent)
        registered_students = set()
        registrations_file = lab / "registrations.csv"
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
            # Touch the student's question directory to bust the leaderboard cache
            # (main.py scans submissions/*/QNO to rebuild leaderboard)
            try:
                q_dir = lab / "submissions" / roll / qno
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
    
        with replace_text(grades_file) as f:
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
