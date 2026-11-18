"""CPU-only synthetic trainer demonstrating the integration protocol."""
import argparse
import json
import math
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--lr", type=float, default=.001)
    parser.add_argument("--delay", type=float, default=.5)
    parser.add_argument("--run-dir", default=os.environ.get("TRAINBOARD_RUN_DIR"))
    args = parser.parse_args()
    run = Path(args.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    (run / "config.json").write_text(json.dumps(vars(args)), encoding="utf8")
    with (run / "metrics.jsonl").open("a", encoding="utf8", buffering=1) as log:
        for epoch in range(1, args.epochs + 1):
            for batch in range(1, 6):
                time.sleep(max(0, args.delay) / 5)
                print(json.dumps(dict(epoch=epoch, epochs=args.epochs, batch=batch, batches=5,
                                      phase="train")), flush=True)
            # Synthetic curves, independent of real datasets or model weights.
            row = dict(epoch=epoch, train_loss=math.exp(-epoch / 3),
                       valid_loss=.15 + math.exp(-epoch / 4), auc=.65 + .2 * (1 - math.exp(-epoch / 3)),
                       f1=.3 + .15 * (1 - math.exp(-epoch / 3)),
                       accuracy=.6 + .2 * (1 - math.exp(-epoch / 3)), lr=args.lr)
            log.write(json.dumps(row) + "\n")
            print(json.dumps(dict(row, epochs=args.epochs, phase="epoch_saved")), flush=True)
    (run / "per_class.csv").write_text("name,auc\nClass A,0.86\nClass B,0.81\nClass C,0.83\n", encoding="utf8")


if __name__ == "__main__":
    main()
