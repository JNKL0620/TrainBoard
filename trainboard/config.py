"""Portable configuration and argument validation."""
import json
import math
import os
import re
import sys
from pathlib import Path


class Config:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        self.raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self.runs = self.resolve(self.raw.get("runs_dir", "runs"))
        self.state = self.resolve(self.raw.get("state_dir", ".trainboard"))
        self.title = self.raw.get("title", "TrainBoard")
        self.metric = self.raw.get("primary_metric", "auc")
        self.mode = self.raw.get("metric_mode", "max")
        if self.mode not in ("min", "max"):
            raise ValueError("metric_mode must be min or max")
        self.entries = self.raw.get("entries", {})
        if not isinstance(self.entries, dict):
            raise ValueError("entries must be an object")
        for key, entry in self.entries.items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", key):
                raise ValueError(f"Invalid entry ID: {key}")
            script = self.resolve(entry["script"])
            if not script.is_file():
                raise ValueError(f"Training script missing: {script}")
            if not isinstance(entry.get("args", []), list):
                raise ValueError("args must be an array of strings")
            for name, spec in entry.get("parameters", {}).items():
                if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name):
                    raise ValueError(f"Invalid parameter: {name}")
                if spec.get("type", "str") not in ("int", "float", "str"):
                    raise ValueError(f"Unsupported parameter type: {name}")
        self.runs.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)

    def resolve(self, value):
        return (self.root / os.path.expandvars(os.path.expanduser(str(value)))).resolve()

    def task(self, payload):
        if not isinstance(payload, dict) or payload.get("entry") not in self.entries:
            raise ValueError("请选择有效训练入口")
        key = payload["entry"]
        entry = self.entries[key]
        specs = entry.get("parameters", {})
        unknown = set(payload) - {"entry"} - specs.keys()
        if unknown:
            raise ValueError("未知参数 " + ", ".join(sorted(unknown)))
        opts = {}
        for name, spec in specs.items():
            value = payload.get(name, spec.get("default"))
            if value is None or value == "":
                continue
            kind = spec.get("type", "str")
            if kind in ("int", "float"):
                if isinstance(value, bool):
                    raise ValueError(f"{name} must be numeric")
                number = float(value)
                if not math.isfinite(number) or (kind == "int" and not number.is_integer()):
                    raise ValueError(f"Invalid {name}")
                if number < spec.get("min", -math.inf) or number > spec.get("max", math.inf):
                    raise ValueError(f"{name} out of range")
                value = int(number) if kind == "int" else number
            else:
                value = str(value)
                if len(value) > 4096:
                    raise ValueError(f"{name} too long")
            if "choices" in spec and value not in spec["choices"]:
                raise ValueError(f"Invalid choice for {name}")
            opts[name] = value
        return entry, opts

    def command(self, job):
        entry = self.entries[job["entry"]]
        argv = [str(self.resolve(entry["python"])) if entry.get("python") else sys.executable,
                "-u", str(self.resolve(entry["script"]))]
        run = self.runs / job["run"]
        placeholders = {"run_dir": str(run), "runs_dir": str(self.runs), "job_id": job["id"]}
        def expand(value):
            text = str(value)
            for key, val in placeholders.items():
                text = text.replace("{" + key + "}", val)
            return text
        argv += [expand(value) for value in entry.get("args", [])]
        for name, value in job["options"].items():
            argv += [entry["parameters"][name].get("flag", "--" + name.replace("_", "-")), str(value)]
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1",
                   TRAINBOARD_RUN_DIR=str(run), TRAINBOARD_JOB_ID=job["id"])
        env.update({k: expand(v) for k, v in entry.get("env", {}).items()})
        return argv, self.resolve(entry.get("cwd", ".")), env

    def public(self):
        metric = {"macro_auc": "auc", "macro_f1": "f1", "val_loss": "valid_loss"}.get(self.metric, self.metric)
        return {"title": self.title, "primary_metric": metric, "metric_mode": self.mode,
                "entries": {key: dict(label=e.get("label", key), group=e.get("group", "训练入口"),
                                      script=e["script"], parameters=e.get("parameters", {}))
                            for key, e in self.entries.items()},
                "presets": {}, "resources": {}}
