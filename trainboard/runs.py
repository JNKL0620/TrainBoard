"""Tolerant readers for CSV / JSONL / existing CXR reports."""
import csv
import json
import math
import os
from pathlib import Path

MARKERS = {"metrics.jsonl", "history.csv", "epoch_metrics.csv", "config.json", "status.json", "run.json", "summary.json"}
ALIASES = {"macro_auc": "auc", "macro_f1": "f1", "f1_at_05": "f1", "val_loss": "valid_loss"}
FIGURES = {"training_curves.png", "per_class_auc.png", "roc_pr.png", "confusion_matrices.png", "confusion_matrix.png"}


def number(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError, OverflowError):
        return None


def read_json(path):
    try:
        v = json.loads(path.read_text(encoding="utf-8-sig"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def csv_rows(path):
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except (OSError, ValueError, UnicodeError):
        return []


def series(path):
    rows = csv_rows(path / "history.csv") + csv_rows(path / "epoch_metrics.csv")
    try:
        with (path / "metrics.jsonl").open(encoding="utf-8-sig") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except ValueError:
                    continue
    except OSError:
        pass
    result = {}
    for row in rows:
        epoch = number(row.get("epoch"))
        if epoch is None or epoch < 0:
            continue
        dest = result.setdefault(epoch, {"epoch": epoch})
        for k, v in row.items():
            n = number(v)
            if n is not None:
                dest[ALIASES.get(k, k)] = n
    return [result[k] for k in sorted(result)]


class Runs:
    def __init__(self, config):
        self.cfg = config

    def path(self, key):
        if not isinstance(key, str) or not key:
            raise ValueError("请选择运行")
        path = (self.cfg.runs / key).resolve()
        if not path.is_relative_to(self.cfg.runs) or path == self.cfg.runs or not path.is_dir():
            raise ValueError("运行路径无效")
        return path

    def describe(self, path, detail=False):
        cfg = read_json(path / "config.json") or read_json(path / "run.json").get("config", {})
        meta = read_json(path / "run.json")
        status = read_json(path / "status.json")
        history = series(path)
        model = cfg.get("model", {})
        if isinstance(model, dict):
            model = model.get("backbone")
        primary = ALIASES.get(self.cfg.metric, self.cfg.metric)
        candidates = [r for r in history if primary in r]
        best = (min if self.cfg.mode == "min" else max)(candidates, key=lambda r: r[primary]) if candidates else {}
        r = dict(id=path.relative_to(self.cfg.runs).as_posix(), name=meta.get("name") or cfg.get("ablation_id") or model or path.name,
                 status=status.get("phase", "completed" if (path / "completed.json").exists() else "recorded"),
                 seed=cfg.get("seed"), input=cfg.get("image_size", cfg.get("data", {}).get("image_size")),
                 epochs=cfg.get("epochs", cfg.get("schedule", {}).get("epochs")),
                 epoch=history[-1]["epoch"] if history else status.get("epoch", 0),
                 auc=best.get(primary), f1=best.get("f1"), best_epoch=best.get("epoch"),
                 mtime=path.stat().st_mtime, primary_metric=primary)
        if detail:
            r.update(metrics=history, classes=[{"name": row.get("name", row.get("class", "")),
                                                "auc": number(row.get("auc"))}
                                               for row in csv_rows(path / "per_class.csv")],
                     figures=[name for name in sorted(FIGURES) if (path / name).is_file()])
        return r

    def list(self):
        result = []
        for root, dirs, files in os.walk(self.cfg.runs):
            path = Path(root)
            dirs[:] = [d for d in dirs if not (path / d).is_symlink()]
            if len(path.relative_to(self.cfg.runs).parts) >= 5:
                dirs.clear()
            if path != self.cfg.runs and MARKERS.intersection(files):
                try:
                    result.append(self.describe(path))
                except OSError:
                    pass
        return sorted(result, key=lambda r: -r["mtime"])


def parse_progress(line):
    try:
        row = json.loads(line)
        if isinstance(row, dict):
            return {k: row[k] for k in ("epoch", "epochs", "batch", "batches", "phase", "detail") if k in row}
    except ValueError:
        pass
    return {}
