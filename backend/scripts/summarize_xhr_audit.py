"""Summarize technopolis_xhr_raw.json for audit markdown."""
import json
import re
from collections import defaultdict
from pathlib import Path

RAW = Path(__file__).resolve().parents[2] / "docs" / "audits" / "technopolis_xhr_raw.json"


def main() -> None:
    d = json.loads(RAW.read_text(encoding="utf-8"))
    api_hosts = defaultdict(list)
    for run in d["runs"]:
        for c in run["candidates"]:
            if "api.technopolis.bg" not in c["url"]:
                continue
            base = re.sub(r"\?.*", "", c["url"])
            api_hosts[base].append(c)

    print("=== api.technopolis.bg unique paths (by product run) ===")
    paths = sorted({re.sub(r"/products/\d+", "/products/{id}", re.sub(r"\?.*", "", c["url"])) for run in d["runs"] for c in run["candidates"] if "api.technopolis.bg" in c["url"]})
    for p in paths:
        print(p)

    print("\n=== product detail endpoints (no references) ===")
    for run in d["runs"]:
        pid = run["product_url"].split("/p/")[-1]
        for c in run["candidates"]:
            u = c["url"]
            if f"/products/{pid}" in u and "references" not in u and "api.technopolis" in u:
                print(run["product_url"][-40:], "->", u[:160])
                print("  signals", c["signals"])
                print("  sample", c["response_sample"][:500].replace("\n", " "))

    print("\n=== httpx replay ===")
    for r in d.get("httpx_replay", []):
        print(r["url"][:120])
        for a in r.get("attempts", []):
            print(" ", {k: a[k] for k in a if k != "sample"})


if __name__ == "__main__":
    main()
