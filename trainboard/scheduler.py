"""Persistent serial dispatcher."""
import json
import os
import subprocess
import threading
import uuid
from datetime import datetime

import psutil

from .runs import parse_progress


def stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Scheduler:
    def __init__(self, config):
        self.cfg = config
        self.path = config.state / "queue.json"
        self.lock = threading.RLock()
        self.proc = self.current = None
        self.enabled = False
        self.closed = threading.Event()
        self.message = "派发已暂停"
        self.jobs = json.loads(self.path.read_text(encoding="utf8")) if self.path.exists() else []
        self.orphans = [job for job in self.jobs if job["state"] in ("running", "stopping", "interrupted")]
        for job in self.orphans:
            job.update(state="interrupted", message="服务曾退出，请检查残留训练进程")
        self.save()
        self.worker = threading.Thread(target=self.dispatch, daemon=True)
        self.worker.start()

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.jobs, ensure_ascii=False, indent=2), encoding="utf8")
        temp.replace(self.path)

    def state(self):
        with self.lock:
            return json.loads(json.dumps(dict(enabled=self.enabled, current=self.current,
                                             message=self.message, jobs=self.jobs)))

    def get(self, ident):
        for job in self.jobs:
            if job["id"] == ident:
                return job
        raise ValueError("任务不存在")

    def add(self, task):
        entry, options = self.cfg.task(task)
        with self.lock:
            if sum(j["state"] == "queued" for j in self.jobs) >= 100:
                raise ValueError("最多排队 100 个任务")
            ident = uuid.uuid4().hex[:12]
            job = dict(id=ident, entry=task["entry"], label=entry.get("label", task["entry"]),
                       group=entry.get("group", "训练"), options=options, state="queued",
                       created=stamp(), progress={}, run=task["entry"] + "_" + ident)
            self.jobs.append(job)
            self.save()
            return ident

    def switch(self, enabled):
        with self.lock:
            if enabled:
                for job in self.orphans:
                    if job.get("pid_created") is None:
                        continue
                    try:
                        process = psutil.Process(job["pid"])
                        if abs(process.create_time() - job["pid_created"]) < .1 and process.is_running():
                            raise ValueError("上次面板的训练进程仍在运行，请等待结束")
                    except (psutil.NoSuchProcess, KeyError):
                        continue
                if not any(j["state"] == "queued" for j in self.jobs):
                    raise ValueError("请先添加任务")
            self.enabled = enabled
            self.message = "顺序运行中" if enabled else "派发已暂停，当前任务继续"

    def queued_action(self, ident, action):
        with self.lock:
            job = self.get(ident)
            if job["state"] != "queued":
                raise ValueError("只能修改排队中的任务")
            if action == "remove":
                self.jobs.remove(job)
            else:
                indices = [i for i, j in enumerate(self.jobs) if j["state"] == "queued"]
                i = indices.index(self.jobs.index(job))
                target = i + (-1 if action == "up" else 1)
                if 0 <= target < len(indices):
                    a, b = indices[i], indices[target]
                    self.jobs[a], self.jobs[b] = self.jobs[b], self.jobs[a]
            self.save()

    def stop(self, ident):
        with self.lock:
            job = self.get(ident)
            if self.current != ident or self.proc is None or self.proc.poll() is not None:
                raise ValueError("任务已结束")
            self.enabled = False
            job["state"] = "stopping"
            self.save()
            if os.name == "nt":
                result = subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                                        capture_output=True, timeout=15,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode and self.proc.poll() is None:
                    raise ValueError("停止失败，请检查进程权限")
            else:
                import signal
                try:
                    root = psutil.Process(self.proc.pid)
                    processes = root.children(recursive=True) + [root]
                except psutil.NoSuchProcess:
                    processes = []
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                _, alive = psutil.wait_procs(processes, timeout=5)
                for process in alive:
                    try:
                        process.kill()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs(alive, timeout=5)

    def log_tail(self, ident):
        with self.lock:
            self.get(ident)
        try:
            with (self.cfg.state / (ident + ".log")).open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 65536))
                return stream.read().decode("utf8", "replace")
        except FileNotFoundError:
            return ""

    def dispatch(self):
        while not self.closed.wait(.3):
            with self.lock:
                if not self.enabled or self.current:
                    continue
                job = next((j for j in self.jobs if j["state"] == "queued"), None)
                if job is None:
                    self.enabled = False
                    self.message = "队列已完成"
                    continue
                minimum = float(self.cfg.raw.get("min_available_gib", 0))
                if psutil.virtual_memory().available < minimum * 1024**3:
                    self.enabled = False
                    self.message = f"可用内存低于 {minimum:g} GiB，派发已暂停"
                    continue
                self.current = job["id"]
                job.update(state="running", started=stamp())
                self.save()
            self.execute(job)

    def execute(self, job):
        exit_code = -1
        run = self.cfg.runs / job["run"]
        try:
            run.mkdir()
            (run / "run.json").write_text(json.dumps({"name": job["label"], "config": job["options"]},
                                                    ensure_ascii=False), encoding="utf8")
            argv, cwd, env = self.cfg.command(job)
            with (self.cfg.state / (job["id"] + ".log")).open("a", encoding="utf8", buffering=1) as log:
                log.write(f"[{stamp()}] {subprocess.list2cmdline(argv)}\n")
                with self.lock:
                    self.proc = subprocess.Popen(
                        argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf8", errors="replace", bufsize=1,
                        start_new_session=os.name != "nt",
                        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0)
                    job["pid"] = self.proc.pid
                    try:
                        job["pid_created"] = psutil.Process(self.proc.pid).create_time()
                    except psutil.NoSuchProcess:
                        job["pid_created"] = None
                    self.save()
                with self.proc.stdout as stream:
                    for line in stream:
                        log.write(line)
                        with self.lock:
                            job["progress"].update(parse_progress(line.strip()))
                exit_code = self.proc.wait()
        except Exception as exc:
            with self.lock:
                job["message"] = str(exc)
            if self.proc is not None:
                self.proc.wait()
        finally:
            with self.lock:
                phase = "stopped" if job["state"] == "stopping" else "completed" if exit_code == 0 else "failed"
                job.update(state=phase, exit_code=exit_code, ended=stamp())
                if run.exists():
                    (run / "status.json").write_text(json.dumps({"phase": phase}), encoding="utf8")
                self.current = self.proc = None
                if phase != "completed":
                    self.enabled = False
                    self.message = "任务停止或失败，派发已暂停"
                self.save()

    def close(self):
        with self.lock:
            self.enabled = False
        self.closed.set()
        self.worker.join()
