"""Offline tests for cmd_digest merge/recovery behaviour (no Gmail, no network)."""
import json, sys, io, types, pathlib, tempfile, contextlib

sys.path.insert(0, str(pathlib.Path(__file__).parent / "scripts"))
import market_digest_cli as cli
cli.JSON_MODE = True

CONFIG = {"email_sources": {"priority_senders": ["Peter Callahan"]}, "gmail": {"label_id": "L1"}}

def mk(i, frm="Someone <a@b.com>"):
    return {"id": i, "from": frm, "subject": f"s{i}", "date": "d", "body": "x" * 10}

def run(tmp, fetched):
    cli.DIGEST_OUTPUT_DIR = tmp
    cli.fetch_new_emails = lambda cfg: [dict(e) for e in fetched]
    cli.label_emails = lambda ids, lid: None
    cli.index_raw_emails = lambda emails, d, cfg: len(emails) * 2
    cli.ingest_funda_emails = lambda emails, cfg: {"ok": True, "status": "empty", "received": 0}
    cli.process_image_queue = lambda: {"ok": True, "status": "ok", "pending_jobs": 0}
    args = types.SimpleNamespace(period="AM")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_digest(args, CONFIG)
    return json.loads(buf.getvalue())

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if not cond and detail else ""))
    if not cond: fails.append(name)

with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    date = None

    # 1. cold start
    out = run(tmp, [mk("a"), mk("b", "Peter Callahan <p@gs.com>")])
    date = out["date"]
    f = tmp / f"digest_{date}_AM.json"
    check("cold start writes 2 emails", out["count"] == 2 and out["status"] == "emails_fetched")
    check("cold start priority counted", out["priority_count"] == 1)
    check("cold start carried_over=0", out["carried_over_count"] == 0)

    # 2. rerun with nothing new -> must NOT report no_new_emails, must hand file back
    out = run(tmp, [])
    check("rerun returns reused_existing", out["status"] == "reused_existing", str(out["status"]))
    check("rerun preserves count", out["count"] == 2, str(out["count"]))
    check("rerun exposes output_file", out.get("output_file") == str(f))
    check("rerun does not truncate file", len(json.load(open(f))["emails"]) == 2)

    # 3. rerun with new emails -> merge, never drop the earlier batch
    out = run(tmp, [mk("c"), mk("a")])  # 'a' is a duplicate
    saved = json.load(open(f))
    ids = [e["id"] for e in saved["emails"]]
    check("merge keeps prior emails", set(ids) == {"a", "b", "c"}, str(ids))
    check("merge dedupes by id", len(ids) == 3, str(len(ids)))
    check("merge preserves order oldest-first", ids == ["a", "b", "c"], str(ids))
    check("merge reports new vs carried", out["new_count"] == 2 and out["carried_over_count"] == 1,
          f'new={out["new_count"]} carried={out["carried_over_count"]}')
    check("merge accumulates chunks", saved["raw_chunks_indexed"] == 4 + 4, str(saved["raw_chunks_indexed"]))

    # 4. genuinely empty slot still reports no_new_emails
    tmp2 = pathlib.Path(td) / "empty"; tmp2.mkdir()
    out = run(tmp2, [])
    check("empty slot still no_new_emails", out["status"] == "no_new_emails" and out["count"] == 0)

    # 5. corrupt prior file must not lose the new fetch
    (tmp2 / f"digest_{date}_AM.json").write_text("{not json")
    out = run(tmp2, [mk("z")])
    check("corrupt prior file degrades safely", out["status"] == "emails_fetched" and out["count"] == 1, str(out))

print()
print(f"{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
