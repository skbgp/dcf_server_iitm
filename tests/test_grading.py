from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sandbox import OutputLimitExceeded, run_bounded
from task import handle_submission


class SandboxProcessTests(unittest.TestCase):
    def test_output_is_bounded_while_the_process_runs(self):
        with self.assertRaises(OutputLimitExceeded):
            run_bounded(
                [sys.executable, "-c", "while True: print('x' * 2048)"],
                timeout=5, memory_mib=256,
            )

    def test_timeout_terminates_process(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=0.2, memory_mib=256,
            )


class GradeReplayTests(unittest.TestCase):
    def test_replay_republishes_a_receipt_once_and_preserves_best_score(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = Path(folder)
            roster = lab / "students.txt"
            roster.write_text("AUDIT1\n")
            (lab / "course.conf").write_text("fm_list=10\ntimeouts=1\n")
            for kind in ("input", "output"):
                case_dir = lab / "testcases" / "Q1" / kind
                case_dir.mkdir(parents=True)
                (case_dir / f"{kind}01.txt").write_text("1\n")

            with patch("task.require_sandbox"), patch("task.grade_attempt") as grade:
                grade.side_effect = [
                    {"status": "Finished", "marks": 10},
                    {"status": "Finished", "marks": 5},
                ]
                args = ("Q1", "AUDIT1", "Q1.cpp", "int main(){}", str(lab), str(roster))
                self.assertEqual(handle_submission.run(*args, attempt="attempt-1")["marks"], 10)
                self.assertEqual(handle_submission.run(*args, attempt="attempt-1")["marks"], 10)
                self.assertEqual(handle_submission.run(*args, attempt="attempt-2")["marks"], 5)
                self.assertEqual(grade.call_count, 2)

            self.assertEqual((lab / "submissions/AUDIT1/Q1/marks.txt").read_text().splitlines(),
                             ["attempt-1, 10", "attempt-2, 5"])
            self.assertIn("AUDIT1,10.0", (lab / "grades.csv").read_text())


if __name__ == "__main__":
    unittest.main()
