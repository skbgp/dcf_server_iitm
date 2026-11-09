"""Atomically admit jobs and retain task ownership in the existing Redis backend."""
import hashlib
import json

OWNER_TTL = 86400
JOB_LEASE = 3600
MAX_PENDING = 150

_ADMIT = """
local now = tonumber(redis.call('TIME')[1])
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
if redis.call('EXISTS', KEYS[2]) == 1 then return 1 end
if redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[5]) then return 2 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
redis.call('ZADD', KEYS[3], now+tonumber(ARGV[4]), ARGV[2])
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
end
redis.call('ZREM', KEYS[2], ARGV[1])
"""


def owner_key(task_id):
    return f"dcf:grading:owner:{task_id}"


def _job_keys(lab, roll):
    lab_key = hashlib.sha256(lab.encode()).hexdigest()
    prefix = f"dcf:grading:{lab_key}"
    return f"{prefix}:roll:{roll}", f"{prefix}:pending"


def admit(client, task_id, lab, roll):
    active, pending = _job_keys(lab, roll)
    owner = json.dumps({"lab": lab, "roll": roll})
    return client.eval(_ADMIT, 3, owner_key(task_id), active, pending,
                       owner, task_id, OWNER_TTL, JOB_LEASE, MAX_PENDING)


def release(client, task_id, lab, roll):
    return client.eval(_RELEASE, 2, *_job_keys(lab, roll), task_id)


def active_job(client, lab, roll):
    active, _ = _job_keys(lab, roll)
    return client.exists(active)
