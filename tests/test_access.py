import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient


class AccessTests(unittest.TestCase):
    def setUp(self):
        previous = Path.cwd()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(os.chdir, previous)
        root = Path(folder.name)
        os.chdir(root)
        active = root / ".active_lab"
        (active / "statics").mkdir(parents=True)
        (active / "students.txt").write_text("AUDIT1\nAUDIT2\n")
        question = active / "submissions/AUDIT1/Q1/attempt"
        question.mkdir(parents=True)
        (question / "Q1_attempt.cpp").write_text("int main() {}")

        self.main = importlib.import_module("main")
        self.old_ip = "10.21.224.110"
        self.new_ip = "10.21.224.111"
        self.lab = str(active.resolve())
        self.backend = Mock()
        self.backend.get_task_meta.return_value = {"status": "SUCCESS", "result": {"marks": 10}}
        self.backend.client.get.return_value = json.dumps({"lab": self.lab, "roll": "AUDIT1"}).encode()
        context = patch.multiple(
            self.main,
            SERVER_IP="127.0.0.1",
            TEST_IP_ENABLED=True,
            ADMIN_PASSWORD=None,
            allowed_systems={"10.21.224.*"},
            ip_roll_map={"AUDIT1": {"ip": self.old_ip, "timestamp": "fixture"}},
            recovery_requests={},
            total_recovery_count=0,
            _student_list_mtime=0.0,
            _student_list_cache=set(),
            capp=SimpleNamespace(backend=self.backend),
        )
        context.start()
        self.addCleanup(context.stop)
        self.client = TestClient(self.main.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)

    def test_task_result_is_checked_against_current_roll_binding(self):
        task = f"/task-status/{uuid4()}"
        self.assertEqual(self.client.get(task, headers={"X-Lab-Test-IP": self.old_ip}).status_code, 200)
        self.assertEqual(self.client.get(task, headers={"X-Lab-Test-IP": self.new_ip}).status_code, 403)

    def test_approved_recovery_rebinds_once_and_hides_admin_list(self):
        replacement = {"X-Lab-Test-IP": self.new_ip}
        old = {"X-Lab-Test-IP": self.old_ip}
        self.assertEqual(self.client.post("/api/request-recovery/Q1/AUDIT1", headers=replacement).status_code, 200)
        self.assertEqual(self.client.get("/api/recovery-requests", headers=replacement).status_code, 403)
        approved = self.client.post("/admin/approve-recovery", params={"roll": "AUDIT1", "qno": "Q1"})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(self.main.ip_roll_map["AUDIT1"]["ip"], self.new_ip)
        self.assertEqual(self.client.get("/api/recover/Q1/AUDIT1", headers=old).status_code, 403)
        self.assertEqual(self.client.get("/api/recover/Q1/AUDIT1", headers=replacement).status_code, 200)
        self.assertEqual(self.client.get("/api/recover/Q1/AUDIT1", headers=replacement).status_code, 403)

    def test_report_destination_is_validated(self):
        path = "/admin/moss-proxy"
        invalid = "http://127.0.0.1:6379/results/audit"
        valid = "https://moss.stanford.edu/results/audit"
        self.assertEqual(self.client.get(path, params={"url": invalid}, follow_redirects=False).status_code, 400)
        response = self.client.get(path, params={"url": valid}, follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], valid)
