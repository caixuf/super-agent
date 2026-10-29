"""Bounded process worker orchestration over the optional FlowCoro executor.

SQLite remains the authoritative queue. Scheduler queues contain only admitted
attempts; process restarts rebuild them from leases, never from in-memory Future.
"""
import os
import threading

from agentcore.scheduler import AgentScheduler


class Worker:
    def __init__(self, pool, command, workers=1, backend='stdlib', timeout=300,
                 lease=60, max_output=1048576, max_file=67108864, poll_interval=0.5):
        if not isinstance(workers, int) or workers < 1:
            raise ValueError('workers must be a positive integer')
        if min(timeout, lease, max_output, max_file, poll_interval) <= 0:
            raise ValueError('budgets and poll_interval must be positive')
        if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
            raise ValueError('command must be a nonempty administrator supplied argv array')
        self.pool, self.command = pool, command
        self.workers, self.backend = workers, backend
        self.timeout, self.lease = timeout, lease
        self.max_output, self.poll_interval = max_output, poll_interval
        self.max_file = max_file

    def run(self, stop=None, drain=False):
        stop = stop if stop is not None else threading.Event()
        active = []
        stats = {'attempts_finished': 0, 'backend': self.backend}
        # Backend initialization deliberately precedes claim: a missing native
        # dependency must not consume attempts or leave jobs running.
        with AgentScheduler(backend=self.backend, workers=self.workers, capacity=self.workers) as scheduler:
            while True:
                while not stop.is_set() and len(active) < self.workers:
                    claim = self.pool.claim(self.lease)
                    if claim is None:
                        break
                    row, fd = claim
                    try:
                        handle = scheduler.submit(row['attempt'], self.pool.run_claimed, claim, self.command,
                                                  timeout=self.timeout, lease=self.lease, max_output=self.max_output,
                                                  max_file=self.max_file)
                    except BaseException:
                        # No execution was admitted. Keep the attempt for normal
                        # lease recovery rather than silently losing ownership.
                        os.close(fd)
                        raise
                    active.append(handle)
                if not active:
                    if stop.is_set() or drain:
                        return stats
                    stop.wait(self.poll_interval)
                    continue
                index = scheduler.wait_first(active, timeout=self.poll_interval)
                if index is not None:
                    handle = active.pop(index)
                    handle.result()
                    stats['attempts_finished'] += 1
