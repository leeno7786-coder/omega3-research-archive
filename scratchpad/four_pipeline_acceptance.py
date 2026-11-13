"""Live acceptance for the shared four-pipeline FIFO (plan Task 8, Step 5).

Submits, through POST /api/chat only:
  1. one bounded build (two tool steps + a ~25 s non-destructive validation command)
  2..4. three independent chat turns while the build's first tool runs
  5. "Change the active build to emit JSON as well as text" — the revision, as an
     ordinary chat turn (never the job mailbox)

While they run it polls /api/telemetry/pipelines every 0.5 s and records which
physical pipeline holds which cycle, then collects every reply from the durable
response rail. Evidence (telemetry trace, replies, the job record) is written to
the directory given as argv[1].

    .venv-brain\\Scripts\\python.exe scratchpad\\four_pipeline_acceptance.py <evidence_dir>
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

HUD = "http://127.0.0.1:13310"
JOBS = Path("guardian/state/jobs")

BUILD = (
    "Build task: create a file named fifo_demo.py in the workspace that prints the numbers 1 to 5, "
    "one per line. Then run this validation command exactly and wait for it: "
    "python -c \"import time; time.sleep(25); print('validated')\" . "
    "Then tell me the file path and the validation output."
)
CHATS = ["What is my dog's name?", "Which editor do I prefer?", "What's the name of my pet?"]
REVISION = "Change the active build to emit JSON as well as text"


def _get(path: str):
    with urllib.request.urlopen(HUD + path, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_chat(text: str) -> dict:
    cycle_id = str(uuid.uuid4())
    body = json.dumps({"text": text, "cycle_id": cycle_id}).encode("utf-8")
    req = urllib.request.Request(HUD + "/api/chat", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    out.setdefault("cycle_id", cycle_id)
    return {"text": text, "cycle_id": out.get("cycle_id") or cycle_id, "submitted_at": time.time(), "response": out}


class Telemetry(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.snapshots: list[dict] = []
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                snap = _get("/api/telemetry/pipelines")
                snap["t"] = round(time.time(), 2)
                self.snapshots.append(snap)
            except Exception as exc:  # noqa: BLE001
                self.snapshots.append({"t": round(time.time(), 2), "error": str(exc)})
            time.sleep(0.5)

    def occupancy(self) -> list[dict]:
        rows = []
        for s in self.snapshots:
            pipes = s.get("pipelines") or {}
            rows.append({
                "t": s["t"],
                "queue_depth": s.get("queue_depth"),
                **{f"P{k}": (v.get("state"), (v.get("cycle_id") or "")[:8], v.get("phase") or "", v.get("job_id") or "") for k, v in sorted(pipes.items())},
            })
        return rows

    def busy_pipeline_for(self, cycle_id: str) -> set[int]:
        out: set[int] = set()
        for s in self.snapshots:
            for k, v in (s.get("pipelines") or {}).items():
                if v.get("cycle_id") == cycle_id and v.get("state") != "free":
                    out.add(int(k))
        return out


def _wait_reply(cycle_id: str, timeout: float) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rail = _get(f"/api/chat/responses?cycle_id={cycle_id}")
            if rail.get("found"):
                return rail
        except Exception:
            pass
        time.sleep(1.0)
    return None


def main(argv: list[str]) -> int:
    evidence = Path(argv[0]) if argv else Path("acceptance_evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    before_jobs = {p.name for p in JOBS.glob("*.json")} if JOBS.exists() else set()
    tele = Telemetry()
    tele.start()
    t0 = time.time()
    turns = [_post_chat(BUILD)]
    print("build submitted", turns[0]["cycle_id"])
    # wait until the build occupies a pipeline and its first tool is running (or 6 s)
    for _ in range(24):
        time.sleep(0.5)
        if tele.busy_pipeline_for(turns[0]["cycle_id"]):
            break
    time.sleep(3.0)
    for text in CHATS:
        turns.append(_post_chat(text))
        print("chat submitted", turns[-1]["cycle_id"], "|", text)
        time.sleep(0.4)
    turns.append(_post_chat(REVISION))
    print("revision submitted", turns[-1]["cycle_id"])
    replies = {}
    for turn in turns:
        reply = _wait_reply(turn["cycle_id"], timeout=240)
        replies[turn["cycle_id"]] = reply
        text = ((reply or {}).get("message") or (reply or {}).get("text") or "")
        print(f"reply [{turn['text'][:40]!r}]: {str(text)[:160]!r}")
    # let the last episodes close, then a final telemetry read
    time.sleep(20)
    tele.stop.set()
    tele.join(timeout=5)
    final = _get("/api/telemetry/pipelines")
    after_jobs = {p.name for p in JOBS.glob("*.json")} if JOBS.exists() else set()
    new_jobs = sorted(after_jobs - before_jobs)
    job_records = {name: json.loads((JOBS / name).read_text(encoding="utf-8")) for name in new_jobs}
    out = {
        "started_at": t0,
        "turns": [{k: v for k, v in t.items() if k != "response"} for t in turns],
        "pipelines_used": {t["cycle_id"]: sorted(tele.busy_pipeline_for(t["cycle_id"])) for t in turns},
        "occupancy_trace": tele.occupancy(),
        "replies": replies,
        "final_telemetry": final,
        "new_job_records": job_records,
    }
    (evidence / "acceptance.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("pipelines used per turn:", out["pipelines_used"])
    print("final queue_depth:", final.get("queue_depth"), "| states:", {k: v.get("state") for k, v in (final.get("pipelines") or {}).items()})
    print("new job records:", new_jobs)
    print("evidence:", evidence / "acceptance.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
