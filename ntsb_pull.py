"""NTSB CAROL puller: every available field for aviation investigations, saved as CSV.

One file, standard library only (Python 3.9+). Two ways to run it:

  1) Sample batch (no arguments): runs every pull in SAMPLES below, writes each to its own
     folder under ntsb_samples_out/, and combines them into ntsb_samples_out/ntsb_samples_all.csv

         python ntsb_pull.py

  2) Single pull (any arguments):

         python ntsb_pull.py --ntsb DCA24MA063
         python ntsb_pull.py --since 2024-01-01 --until 2024-06-30 --part121 --completed
         python ntsb_pull.py --catalog-only

     Options: --since/--until YYYY-MM-DD (default last 30 days), --ntsb NUMBER, --part121,
     --completed, --no-recs, --catalog-only, --out FOLDER (default ntsb_out),
     --min N (default 25: the date window is widened backward until at least N cases are found),
     --step-days (default 30), --max-steps (default 36)

Endpoints (the same public ones https://my.ntsb.gov uses):
  POST /Carol/v1/GetCustomSearchJson      full case records (~115 fields, nested vehicles/engines/
                                          findings/events/injuries, narratives, probable cause)
  POST /Carol/v1/GetCustomSafetyRecsJson  safety recommendations linked to the matching cases
  GET  /Carol/v1/GetCustomFilterOptions   catalog of every searchable field
  POST /Carol/v2/GetInvestigationsCustom  summary rows (the search results table)

The API needs the Ocp-Apim-Subscription-Key header. NTSB publishes the key in the site's own
JavaScript bundle; it is read from there at run time, or from NTSB_CAROL_KEY if you set it.

Files per pull: cases_raw.json, cases.csv, vehicles.csv, engines.csv, findings.csv, events.csv,
injuries.csv, summary.csv, field_catalog.csv, safety_recs_raw.json, safety_recs.csv,
safety_rec_addressees.csv
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.request

SITE = "https://my.ntsb.gov"
MIN_SAMPLES = 25          # every date-range pull returns at least this many cases (see --min)
API = "https://api.ntsb.gov/searchpub/api/Carol/"
UA = "Mozilla/5.0 (ntsb_samples.py; research use)"


# ---------------------------------------------------------------- HTTP helpers
def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode("utf-8", "replace")


def find_key():
    key = os.environ.get("NTSB_CAROL_KEY")
    if key:
        return key
    html = _get(SITE + "/")
    m = re.search(r'/assets/index-[A-Za-z0-9_-]+\.js', html)
    if not m:
        sys.exit("Could not locate the CAROL JavaScript bundle; set NTSB_CAROL_KEY instead.")
    bundle = _get(SITE + m.group(0))
    k = re.search(r'carolApiSubscriptionKey\s*:\s*["\']([0-9a-fA-F]{20,})["\']', bundle)
    if not k:
        sys.exit("Could not read the API key from the CAROL bundle; set NTSB_CAROL_KEY instead.")
    return k.group(1)


class Carol:
    def __init__(self, key):
        self.h = {"Content-Type": "application/json", "Accept": "application/json",
                  "Ocp-Apim-Subscription-Key": key, "User-Agent": UA,
                  "Origin": SITE, "Referer": SITE + "/"}

    def post(self, path, body):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                     headers=self.h, method="POST")
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def get(self, path):
        return json.loads(_get(API + path, self.h))


# ---------------------------------------------------------------- query builder
def crit(field, op, value):
    return {"operator": "All", "criteria": [{"field": field, "operator": op, "value": value}]}


def build_body(a=None, page_size=500, start=0, since=None, until=None, ntsb=None):
    ntsb = ntsb or (a.ntsb if a else None)
    since = since or (a.since if a else None)
    until = until or (a.until if a else None)
    assoc = [crit("casedetail.TopicMode", "Contains", "Aviation")]
    if ntsb:
        assoc.append(crit("NTSBNumber", "Contains", ntsb))
    else:
        assoc.append(crit("EventDate", "GreaterThanEqualTo", since))
        assoc.append(crit("EventDate", "LessThanEqualTo", until))
    return {"userName": "anonymous",
            "paging": {"pageSize": page_size, "startIndex": start},
            "sorting": {"sortingFilters": []},
            "filter": {"operator": "All", "groups": [{"operator": "All", "associations": assoc}]},
            "timeZone": "UTC"}


# ---------------------------------------------------------------- flattening
def scalar(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def write_csv(path, rows):
    if not rows:
        return 0
    cols = []
    for r in rows:                      # union of keys, first-seen order
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: scalar(r.get(k)) for k in cols})
    return len(rows)


def flatten_cases(cases):
    tables = {"cases": [], "vehicles": [], "engines": [], "findings": [], "events": [], "injuries": []}
    for c in cases:
        tables["cases"].append({k: v for k, v in c.items() if not isinstance(v, list)})
        for v in c.get("Vehicles") or []:
            base = {"NTSBNumber": c.get("NTSBNumber")}
            for vid in v.get("VehicleIds") or []:
                tables["vehicles"].append({**base, **vid})
            for name, key in (("engines", "Engines"), ("findings", "Findings"),
                              ("events", "Events"), ("injuries", "Injuries")):
                for item in v.get(key) or []:
                    tables[name].append({**base, **item})
    return tables


def flatten_recs(recs):
    rows, addr = [], []
    for r in recs:
        rows.append({k: v for k, v in r.items() if not isinstance(v, list)}
                    | {"Codings": r.get("SafteyRecCodings"), "Notations": r.get("Notations")})
        for a in r.get("Addressees") or []:
            addr.append({"Srid": r.get("Srid"), **a} if isinstance(a, dict) else {"Srid": r.get("Srid"), "Addressee": a})
    return rows, addr


def flatten_catalog(nodes):
    out = []

    def walk(n, path):
        here = path + [n.get("displayName") or ""]
        if n.get("isField"):
            out.append({"group": " > ".join(here[:-1]), "fieldName": n.get("fieldName"),
                        "displayName": n.get("displayName"), "type": n.get("type"),
                        "operators": "|".join(o["operator"]["operation"] for o in n.get("fieldOperatorOptions") or []
                                              if o.get("operator")),
                        "optionCount": len(n.get("fieldOptions") or [])})
        for ch in n.get("children") or []:
            walk(ch, here)
    for n in nodes:
        walk(n, [])
    return out


def is_part121(c):
    return any(str(vid.get("RegulationFlightConductedUnder", "")).strip() == "121"
               for v in c.get("Vehicles") or [] for vid in v.get("VehicleIds") or [])


# ---------------------------------------------------------------- main
def pull(argv=None):
    today = dt.date.today()
    p = argparse.ArgumentParser(description="Pull NTSB aviation cases (all fields) from CAROL.", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", default=(today - dt.timedelta(days=30)).isoformat())
    p.add_argument("--until", default=today.isoformat())
    p.add_argument("--ntsb", help="single NTSB number, e.g. DCA24MA063")
    p.add_argument("--part121", action="store_true", help="keep only Part 121 (airline) cases")
    p.add_argument("--completed", action="store_true", help="keep only completed cases")
    p.add_argument("--no-recs", action="store_true", help="skip safety recommendations")
    p.add_argument("--catalog-only", action="store_true")
    p.add_argument("--min", type=int, default=MIN_SAMPLES,
                   help=f"minimum cases to return; the date window is widened backward until reached "
                        f"(default {MIN_SAMPLES}, 0 = no minimum)")
    p.add_argument("--step-days", type=int, default=30, help="how far back each widening step goes")
    p.add_argument("--max-steps", type=int, default=36, help="stop widening after this many steps")
    p.add_argument("--out", default="ntsb_out")
    a = p.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)

    carol = Carol(find_key())

    catalog = flatten_catalog(carol.get("v1/GetCustomFilterOptions"))
    n = write_csv(os.path.join(a.out, "field_catalog.csv"), catalog)
    print(f"field_catalog.csv      {n} searchable fields")
    if a.catalog_only:
        return

    def keep(c):
        if a.part121 and not is_part121(c):
            return False
        if a.completed and not str(c.get("CompletionStatus", "")).lower().startswith("complet"):
            return False
        return True

    if a.ntsb:
        cases = [c for c in carol.post("v1/GetCustomSearchJson", build_body(a)) if keep(c)]
    else:
        # pull the requested window, then step backward one window at a time until --min is met
        found, since, until, steps = {}, a.since, a.until, 0
        while True:
            batch = carol.post("v1/GetCustomSearchJson", build_body(since=since, until=until))
            for c in batch:
                if keep(c):
                    found.setdefault(c.get("NTSBNumber"), c)
            print(f"  window {since} .. {until}: {len(batch)} cases, {len(found)} kept so far")
            if len(found) >= a.min or steps >= a.max_steps:
                break
            steps += 1
            new_until = dt.date.fromisoformat(since) - dt.timedelta(days=1)
            until, since = new_until.isoformat(), (new_until - dt.timedelta(days=a.step_days - 1)).isoformat()
        if since != a.since:
            print(f"  widened start date {a.since} -> {since} to reach {a.min} cases")
            a.since = since
        cases = sorted(found.values(), key=lambda c: str(c.get("EventDateTimeUTC") or ""), reverse=True)
        if len(cases) < a.min:
            print(f"  !! only {len(cases)} cases found (wanted {a.min}) after {steps} widening steps")
    with open(os.path.join(a.out, "cases_raw.json"), "w", encoding="utf-8") as f:
        json.dump(cases, f, indent=2, ensure_ascii=False)

    for name, rows in flatten_cases(cases).items():
        n = write_csv(os.path.join(a.out, f"{name}.csv"), rows)
        cols = len({k for r in rows for k in r})
        print(f"{name + '.csv':22} {n} rows x {cols} columns")

    summary = carol.post("v2/GetInvestigationsCustom", build_body(a, page_size=5000))
    wanted = {c.get("NTSBNumber") for c in cases}
    rows = [r for r in summary.get("results", []) if r.get("ntsbNumber") in wanted]
    write_csv(os.path.join(a.out, "summary.csv"), rows)
    print(f"summary.csv            {len(rows)} rows")

    with_recs = [c.get("NTSBNumber") for c in cases if str(c.get("hasSafetyRecommendation")).lower() == "true"]
    if not a.no_recs and with_recs:
        recs = []
        for num in with_recs:                      # only the cases that actually have recommendations
            recs += carol.post("v1/GetCustomSafetyRecsJson", build_body(ntsb=num)) or []
        with open(os.path.join(a.out, "safety_recs_raw.json"), "w", encoding="utf-8") as f:
            json.dump(recs, f, indent=2, ensure_ascii=False)
        r_rows, addr = flatten_recs(recs)
        write_csv(os.path.join(a.out, "safety_recs.csv"), r_rows)
        write_csv(os.path.join(a.out, "safety_rec_addressees.csv"), addr)
        print(f"safety_recs.csv        {len(r_rows)} recommendations")

    print(f"\nDone -> {os.path.abspath(a.out)}")


# ================================================================ sample batch

OUT_ROOT = "ntsb_samples_out"
TODAY = dt.date.today()

# ------------------------------------------------------------------ inputs
# Each entry = one pull. Leave a value as None to use the script's default.
# Every date-range pull returns at least MIN_SAMPLES (25) cases: if the window is too narrow,
# it is widened backward 30 days at a time until 25 are found. Add "min": N to override per pull.
SAMPLES = [
    {   # one well-known airline case, every field + its 19 safety recommendations
        "name": "single_case_DCA24MA063",
        "ntsb": "DCA24MA063",
    },
    {   # completed airline (Part 121) cases from H1 2024
        "name": "part121_completed_2024H1",
        "since": "2024-01-01", "until": "2024-06-30",
        "part121": True, "completed": True,
    },
    {   # a small window of completed cases of all types (GA, Part 135, etc.)
        "name": "all_completed_2024_03_01_to_10",
        "since": "2024-03-01", "until": "2024-03-10",
        "completed": True,
    },
    {   # the most recent 14 days (mostly preliminary, no probable cause yet)
        "name": "recent_14_days",
        "since": (TODAY - dt.timedelta(days=14)).isoformat(), "until": TODAY.isoformat(),
        "no_recs": True,
    },
]
# --------------------------------------------------------------------------


def args_for(s):
    """Turn one SAMPLES entry into ntsb_samples.py command-line arguments."""
    argv = ["--out", os.path.join(OUT_ROOT, s["name"])]
    for key in ("since", "until", "ntsb"):
        if s.get(key):
            argv += [f"--{key}", s[key]]
    if s.get("min") is not None:                  # per-sample override of MIN_SAMPLES
        argv += ["--min", str(s["min"])]
    for flag in ("part121", "completed", "no_recs"):
        if s.get(flag):
            argv.append("--" + flag.replace("_", "-"))
    return argv


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def combine(sample_names, out_path):
    """One row per case with the most useful fields from every child table."""
    rows = []
    for name in sample_names:
        folder = os.path.join(OUT_ROOT, name)
        cases = read_csv(os.path.join(folder, "cases.csv"))
        by = {}
        for table in ("vehicles", "engines", "findings", "events", "injuries"):
            for r in read_csv(os.path.join(folder, f"{table}.csv")):
                by.setdefault((table, r.get("NTSBNumber")), []).append(r)

        for c in cases:
            n = c.get("NTSBNumber")
            veh = by.get(("vehicles", n), [{}])
            eng = by.get(("engines", n), [{}])
            fnd = by.get(("findings", n), [])
            evs = by.get(("events", n), [])
            inj = by.get(("injuries", n), [])
            defining = next((e for e in evs if str(e.get("IsDefiningEvent")).lower() == "true"), {})
            pc = [f for f in fnd if str(f.get("InProbableCause")).lower() == "true"]

            row = {"sample": name}
            row.update(c)                                   # every case-level field
            for k, v in veh[0].items():                     # first aircraft, all identity fields
                if k not in ("NTSBNumber", "Mkey", "VehicleNumber"):
                    row["aircraft_" + k] = v
            row["aircraft_count"] = len(veh) if veh != [{}] else 0
            for k in ("EngineType", "EngineManufacturer", "EngineModelSeries"):
                row["engine1_" + k] = eng[0].get(k)
            row["defining_event"] = " > ".join(filter(None, [defining.get("Tier1Name"), defining.get("Tier2Name")]))
            row["defining_event_cictt_category"] = defining.get("CICTTEventSOEGroup")
            row["defining_event_phase"] = defining.get("CICTTPhaseSOEGroup")
            row["event_sequence"] = " | ".join(
                f"{e.get('SequenceNumber')}:{e.get('Tier2Name') or e.get('Tier1Name')}"
                for e in sorted(evs, key=lambda e: int(e.get("SequenceNumber") or 0)))
            row["finding_count"] = len(fnd)
            row["probable_cause_findings"] = " | ".join(
                f"{f.get('FindingCode')} {f.get('FindingText') or ''}".strip() for f in pc)
            row["all_finding_codes"] = " | ".join(f.get("FindingCode", "") for f in fnd)
            for r in inj:                                   # injury counts, summed across aircraft
                for k, v in r.items():
                    if k.startswith("Aviation"):
                        try:
                            row["inj_" + k] = row.get("inj_" + k, 0) + int(v or 0)
                        except ValueError:
                            pass
            rows.append(row)

    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows), len(cols)


def run_samples():
    os.makedirs(OUT_ROOT, exist_ok=True)
    done = []
    for s in SAMPLES:
        print(f"\n=== {s['name']} ===")
        argv = args_for(s)
        print("inputs:", " ".join(argv))
        try:
            pull(argv)
            done.append(s["name"])
        except Exception as e:                     # keep going if one pull fails
            print(f"!! {s['name']} failed: {e}")

    out = os.path.join(OUT_ROOT, "ntsb_samples_all.csv")
    n, cols = combine(done, out)
    print(f"\nCombined file: {out}  ({n} cases x {cols} columns from {len(done)} of {len(SAMPLES)} pulls)")
    if n < MIN_SAMPLES:
        print(f"!! fewer than {MIN_SAMPLES} cases in the combined file; widen a window in SAMPLES")
    with open(os.path.join(OUT_ROOT, "inputs_used.json"), "w") as f:
        json.dump({"run_at": dt.datetime.now().isoformat(timespec="seconds"), "samples": SAMPLES}, f, indent=2)

def main():
    if len(sys.argv) > 1:
        pull(sys.argv[1:])
    else:
        run_samples()


if __name__ == "__main__":
    main()
