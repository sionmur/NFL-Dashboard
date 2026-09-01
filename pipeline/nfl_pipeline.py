# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
nfl_pipeline.py -- een script voor de volledige NFL-dashboard pipeline.

Vervangt en verenigt de losse scripts:
    fetch_roster.py       fetch_stats.py        fetch_playcontext.py
    fetch_odds.py         build_season.py       add_playcontext.py
    build_all.py

WAT HET OPHAALT (gratis, via nflverse)
    roster, speelschema, wekelijkse spelersstatistieken, snap counts,
    play-by-play, FTN-charting
WAT HET DAARUIT AFLEIDT
    play_context        speelsituaties per speler (shotgun, blitz, red zone, ...)
    team_def_weekly     wat een verdediging toestaat per positiegroep
    run_def_weekly      rush yards toegestaan + gemiddelde box count
    rb_rush_weekly      carries en rushing yards per RB
    form_profile_weekly shotgun/under center per team
WAT HET NIET KAN AFLEIDEN (overnemen met --keep-from)
    player_cov_weekly, def_profile_weekly, def_pos_weekly, form_prod_weekly,
    injuries, vacated -- coverage-charting zit niet in nflverse.

ODDS
    Player-prop odds komen van the-odds-api.com. Elke run van 'odds' schrijft
    een NIEUW snapshot met tijdstempel; niets wordt ooit overschreven, want een
    line beweegt gedurende de week. 'build' leest alle snapshots, pakt per
    (speler, markt, week) de laatste voor kickoff (de closing line) en wikkelt
    die af tegen de werkelijke statistiek.

------------------------------------------------------------------ COMMANDO'S

  1. Odds-snapshot ophalen  (meerdere keren per week: di / do / zo)
     set ODDS_API_KEY=...
     python nfl_pipeline.py odds --week 3
        -> data/raw_odds/odds_<seizoen>_wk03_<tijdstempel>.json

  2. Volledig seizoensbestand bouwen
     python nfl_pipeline.py build --season 2025 --out dashboard_data_2025.js
        --keep-from dashboard_data_2025.js   tabellen overnemen die dit script
                                             niet kan maken (+ bewaart .bak)
        --allow-unmatched                    doorgaan ondanks te veel niet-
                                             gekoppelde odds-namen

  3. Alleen speelsituaties bijwerken in een bestaand bestand
     python nfl_pipeline.py context --season 2025 --data dashboard_data_2025.js
        laat al het andere staan, herberekent alleen play_context (+ .bak)

  4. Wekelijkse update  (voor de geplande taak op donderdagochtend)
     python nfl_pipeline.py weekly
        bepaalt zelf seizoen + eerstvolgende week uit het speelschema, haalt een
        odds-snapshot (als ODDS_API_KEY gezet is) en herbouwt
        dashboard_data_<seizoen>.js. Logt naar weekly_update.log.

VEREIST:  pip install pandas      (pyarrow is optioneel: fallback voor FTN)
"""

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

# ====================================================================
#  Constanten
# ====================================================================

BASE = "https://github.com/nflverse/nflverse-data/releases/download"
URLS = {
    "roster":   BASE + "/rosters/roster_{s}.csv",
    "stats":    BASE + "/stats_player/stats_player_week_{s}.csv",
    "snaps":    BASE + "/snap_counts/snap_counts_{s}.csv",
    "pbp":      BASE + "/pbp/play_by_play_{s}.csv.gz",
    "ftn_csv":  BASE + "/ftn_charting/ftn_charting_{s}.csv",
    "ftn":      BASE + "/ftn_charting/ftn_charting_{s}.parquet",
}
SCHEDULE_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

SKILL = {"QB", "RB", "FB", "WR", "TE"}
# ACT = actief, RES = injured reserve (kan terugkomen), E14 = practice-squad-achtig.
ACTIEF = {"ACT", "RES", "E14"}

# nflverse gebruikt in het rosterbestand soms een andere teamcode dan in de
# statistieken (AZ vs ARI). Zonder deze omzetting koppelt zo'n team stil niet.
TEAM_FIX = {
    "AZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    "SL": "LA", "STL": "LA", "LAR": "LA", "SD": "LAC",
    "OAK": "LV", "RAI": "LV", "WSH": "WAS", "WFT": "WAS", "JAC": "JAX",
}

# Tabellen die dit script niet kan maken (coverage-charting). Met --keep-from
# worden ze uit het bestaande bestand overgenomen.
NIET_AFLEIDBAAR = [
    "player_cov_weekly", "def_profile_weekly", "def_pos_weekly",
    "form_prod_weekly", "injuries", "vacated", "odds",
]

SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")

# ---- odds ----------------------------------------------------------
API_BASE = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"

# De markten die het dashboard gebruikt. Uitbreiden mag, maar voeg ze dan ook
# toe aan ODDS_LABEL in dashboard.html en aan STAT_FOR_MARKET hieronder.
MARKETS = [
    "player_reception_yds",
    "player_receptions",
    "player_rush_yds",
]

# Welke statistiek hoort bij welke markt (voor het afwikkelen).
STAT_FOR_MARKET = {
    "player_reception_yds": "rec_yds",
    "player_receptions": "rec",
    "player_rush_yds": "rush_yds",
}

# Een boek als referentie. Meerdere boeken door elkaar maakt je logboek
# oncontroleerbaar: je weet dan niet meer welke prijs je keuze rechtvaardigde.
BOOKMAKER = os.environ.get("ODDS_BOOKMAKER", "draftkings")
RAW_DIR = Path(os.environ.get("ODDS_RAW_DIR", "data/raw_odds"))

# Hoeveel procent niet-gekoppelde odds-namen we accepteren voor we ingrijpen.
MAX_UNMATCHED_PCT = 5.0


# ====================================================================
#  Gedeelde helpers
# ====================================================================

def fix_team(t):
    t = str(t or "").strip().upper()
    return TEAM_FIX.get(t, t)


def norm_name(s):
    """Normaliseert een spelersnaam zodat bronnen op elkaar aansluiten.

    Moet exact overeenkomen met normName() in dashboard.html.
    """
    s = str(s or "").lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = SUFFIXES.sub("", s)
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def abbrev_key(name):
    """'Travis Kelce' en 'T.Kelce' worden allebei 't kelce'.

    Play-by-play kort voornamen af tot een letter met een punt zonder spatie
    ('J.Conner'). De punt wordt eerst een spatie, zo komen 'James Conner' en
    'J.Conner' allebei uit op 'j conner'.
    """
    n = norm_name(str(name or "").replace(".", " "))
    if not n:
        return ""
    d = n.split()
    return d[0] if len(d) == 1 else d[0][0] + " " + " ".join(d[1:])


def f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def i(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def haal(url, verplicht=True, wat=""):
    try:
        with urlopen(url, timeout=240) as r:
            return r.read()
    except HTTPError as e:
        if e.code == 404:
            if verplicht:
                sys.exit(f"Niet gevonden: {wat or url}\n"
                         f"Voor een seizoen dat nog niet begonnen is, is dit normaal.")
            print(f"  (niet beschikbaar: {wat}, wordt overgeslagen)", file=sys.stderr)
            return None
        raise


def lees_csv(data):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8", "replace"))))


def build_abbrev_index(namen, team_van):
    """Bouwt {(team, afkorting): volledige naam}.

    Per team, want binnen een team is een afkorting vrijwel altijd uniek;
    competitiebreed botst 'J.Smith' te vaak. Botsingen binnen een team laten
    we vallen: liever een ontbrekende regel dan een verkeerd toegewezen regel.
    """
    index, botsing = {}, set()
    for naam in namen:
        t = team_van.get(norm_name(naam))
        if not t:
            continue
        k = (t, abbrev_key(naam))
        if k in index and index[k] != naam:
            botsing.add(k)
        index[k] = naam
    for k in botsing:
        index.pop(k, None)
    return index, botsing


_SEIZOEN_RE = re.compile(
    r"(window\.NFL_SEASONS\[['\"](\d+)['\"]\]\s*=\s*)(\{.*\})(\s*;?\s*)$", re.S)


def split_season_file(tekst):
    """Splitst een dashboard_data_<jaar>.js in (kop, prefix, seizoen, data, staart).

    'kop' is alles voor de toewijzing -- daar staat meestal
    'window.NFL_SEASONS = window.NFL_SEASONS || {};'. Zonder die regel is het
    bestand kapot.
    """
    m = _SEIZOEN_RE.search(tekst)
    if not m:
        sys.exit("Kon de seizoensdata niet uit dit bestand lezen. Verwacht een "
                 "regel als: window.NFL_SEASONS['2025'] = {...};")
    kop = tekst[:m.start()]
    prefix, seizoen, blob, staart = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    return kop, prefix, seizoen, json.loads(blob), (staart or ";\n")


def lees_bestaand(path):
    """Haalt de seizoensdata uit een bestaand dashboard_data_<jaar>.js."""
    p = Path(path)
    if not p.exists():
        print(f"  --keep-from: {p} bestaat niet, overgeslagen", file=sys.stderr)
        return {}
    m = re.search(r"window\.NFL_SEASONS\[['\"](\d+)['\"]\]\s*=\s*(\{.*\})\s*;?\s*$",
                  p.read_text(), re.S)
    if not m:
        print(f"  --keep-from: kon {p} niet lezen, overgeslagen", file=sys.stderr)
        return {}
    return json.loads(m.group(2))


def schrijf_seizoen(out, seizoen, data, backup=True):
    out = Path(out)
    if out.exists() and backup:
        bak = out.with_suffix(out.suffix + ".bak")
        shutil.copy(out, bak)
        print(f"back-up: {bak}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "window.NFL_SEASONS = window.NFL_SEASONS || {};\n"
        f"window.NFL_SEASONS['{seizoen}'] = "
        + json.dumps(data, separators=(",", ":")) + ";\n"
    )


# ====================================================================
#  Ophalen + afleiden uit nflverse
# ====================================================================

def bouw_roster(season):
    print("roster...")
    raw = haal(URLS["roster"].format(s=season), verplicht=False, wat="roster")
    if raw is None:
        return []
    laatste = {}
    for r in lees_csv(raw):
        naam = (r.get("full_name") or "").strip()
        pos = (r.get("position") or "").strip().upper()
        if not naam or pos not in SKILL:
            continue
        if (r.get("status") or "").strip().upper() not in ACTIEF:
            continue
        wk = i(r.get("week"))
        v = laatste.get(naam)
        if v is None or wk >= v["_wk"]:
            laatste[naam] = {"player": naam, "team": fix_team(r.get("team")),
                             "pos": pos, "_wk": wk}
    out = sorted(({"player": v["player"], "team": v["team"], "pos": v["pos"]}
                  for v in laatste.values() if v["team"]),
                 key=lambda x: (x["team"], x["pos"], x["player"]))
    teams = len({r["team"] for r in out})
    print(f"  {len(out)} spelers, {teams} teams")
    if out and len(out) / max(teams, 1) > 30:
        print("  LET OP: gemiddeld >30 skill-spelers per team -- dat wijst op een "
              "voorseizoensroster (90 man). Draai opnieuw na de roster cuts eind "
              "augustus en nog eens na week 1.", file=sys.stderr)
    return out


def bouw_schedule(season):
    print("speelschema...")
    with urlopen(SCHEDULE_URL, timeout=120) as r:
        rows = lees_csv(r.read())
    uit = [{"week": i(g["week"]), "away_team": fix_team(g.get("away_team")),
            "home_team": fix_team(g.get("home_team")), "gameday": g.get("gameday") or ""}
           for g in rows
           if str(g.get("season")) == str(season)
           and (g.get("game_type") or "REG").upper() == "REG"]
    uit.sort(key=lambda g: (g["week"], g["gameday"], g["home_team"]))
    print(f"  {len(uit)} wedstrijden")
    return uit


def bouw_stats(season):
    print("spelersstatistieken...")
    raw = haal(URLS["stats"].format(s=season), wat="statistieken")
    stats = lees_csv(raw)

    print("snap counts...")
    snaps_raw = haal(URLS["snaps"].format(s=season), verplicht=False, wat="snap counts")
    snap_idx = {}
    for r in (lees_csv(snaps_raw) if snaps_raw else []):
        pct = f(r.get("offense_pct"))
        if pct > 1.5:                       # sommige jaren staat het als 0-100
            pct /= 100.0
        snap_idx[(i(r.get("week")), fix_team(r.get("team")),
                  (r.get("player") or "").strip())] = pct

    rijen = []
    for r in stats:
        if (r.get("season_type") or "REG").upper() != "REG":
            continue
        pos = (r.get("position") or "").strip().upper()
        if pos not in SKILL:
            continue
        naam = (r.get("player_display_name") or r.get("player_name") or "").strip()
        if not naam:
            continue
        wk, team = i(r.get("week")), fix_team(r.get("team"))
        rijen.append({
            "season": season, "week": wk, "team": team, "player": naam, "pos": pos,
            "targets": f(r.get("targets")), "rec": f(r.get("receptions")),
            "rec_yds": f(r.get("receiving_yards")), "rush_yds": f(r.get("rushing_yards")),
            "snap_pct": snap_idx.get((wk, team, naam)),
        })

    # target share = aandeel in de targets van het eigen team die week
    tot = defaultdict(float)
    for r in rijen:
        tot[(r["week"], r["team"])] += r["targets"]
    for r in rijen:
        t = tot[(r["week"], r["team"])]
        r["tgt_share"] = (r["targets"] / t) if t > 0 else 0.0

    zonder = sum(1 for r in rijen if r["snap_pct"] is None)
    print(f"  {len(rijen)} regels, {zonder} zonder snap%")
    return rijen


def bouw_uit_pbp(season, stats, roster):
    """Leidt speelsituaties en verdedigingstabellen af uit play-by-play + FTN."""
    try:
        import pandas as pd
    except ImportError:
        sys.exit("pandas is vereist:\n  pip install pandas")
    try:
        import pyarrow.parquet as pq          # alleen als reserve voor de CSV
    except ImportError:
        pq = None

    print("play-by-play...")
    raw = haal(URLS["pbp"].format(s=season), wat="play-by-play")
    cols = ["game_id", "play_id", "week", "posteam", "defteam", "play_type",
            "receiver_player_name", "rusher_player_name", "complete_pass",
            "yards_gained", "pass_length", "down", "yardline_100", "shotgun",
            "rush_attempt", "pass_attempt"]
    pbp = pd.read_csv(io.BytesIO(raw), compression="gzip", low_memory=False,
                      usecols=lambda c: c in cols)
    print(f"  {len(pbp)} plays")

    print("FTN charting...")
    # CSV eerst: het parquet-bestand is met een nieuwere schrijver gemaakt dan
    # sommige pyarrow-versies aankunnen ("Repetition level histogram size
    # mismatch"). De CSV bevat dezelfde gegevens en werkt overal.
    ftn = None
    craw = haal(URLS["ftn_csv"].format(s=season), verplicht=False, wat="FTN (csv)")
    if craw is not None:
        try:
            ftn = pd.read_csv(io.BytesIO(craw), low_memory=False)
        except Exception as e:
            print(f"  CSV lezen mislukt ({e}), parquet proberen...", file=sys.stderr)
    if ftn is None:
        praw = haal(URLS["ftn"].format(s=season), verplicht=False, wat="FTN (parquet)") if pq else None
        if praw is not None:
            try:
                ftn = pq.read_table(io.BytesIO(praw)).to_pandas()
            except Exception as e:
                print(f"  parquet lezen mislukt: {e}", file=sys.stderr)

    if ftn is not None:
        # de vlaggen komen uit CSV als tekst binnen: naar echte booleans
        for c in ["is_play_action"]:
            if c in ftn.columns and ftn[c].dtype == object:
                ftn[c] = ftn[c].astype(str).str.strip().str.lower().isin(
                    ["true", "1", "1.0", "yes"])
        for c in ["n_blitzers", "n_defense_box"]:
            if c in ftn.columns:
                ftn[c] = pd.to_numeric(ftn[c], errors="coerce")
        keep = ["nflverse_game_id", "nflverse_play_id", "is_play_action",
                "n_blitzers", "n_defense_box"]
        keep = [c for c in keep if c in ftn.columns]
        m = pbp.merge(ftn[keep], left_on=["game_id", "play_id"],
                      right_on=["nflverse_game_id", "nflverse_play_id"], how="left")
        for c in ["is_play_action", "n_blitzers", "n_defense_box"]:
            if c not in m.columns:
                m[c] = None
        gek = m["nflverse_game_id"].notna().sum()
        print(f"  {len(ftn)} gecharte plays, gekoppeld: {gek} "
              f"({100*gek/max(1,len(pbp)):.1f}%)")
    else:
        m = pbp.copy()
        for c in ["is_play_action", "n_blitzers", "n_defense_box"]:
            m[c] = None
        print("  geen FTN beschikbaar: play-action, blitz en box-count blijven leeg",
              file=sys.stderr)

    m["off"] = m.posteam.map(fix_team)
    m["dteam"] = m.defteam.map(fix_team)

    # ---- speelsituaties per ontvanger ----
    p = m[(m.play_type == "pass") & m.receiver_player_name.notna()].copy()
    p["rec"] = (p.complete_pass == 1).astype(int)
    ctx = {
        "shotgun":      p.shotgun == 1,
        "under_center": p.shotgun == 0,
        "play_action":  p.is_play_action == True,       # noqa: E712
        "blitz":        p.n_blitzers >= 1,
        "third_down":   p.down == 3,
        "deep":         p.pass_length == "deep",
        "stacked_box":  p.n_defense_box >= 7,
        "red_zone":     p.yardline_100 <= 20,
    }
    # afkortingen -> volledige namen (per team, botsingen laten vallen)
    team_van = {}
    for r in stats:
        if r.get("team"):
            team_van.setdefault(norm_name(r["player"]), r["team"])
    for r in roster:
        team_van.setdefault(norm_name(r["player"]), r["team"])
    namen = {r["player"] for r in stats} | {r["player"] for r in roster}
    idx, _bots = build_abbrev_index(namen, team_van)

    play_context, mis = [], set()
    for naam_ctx, masker in ctx.items():
        sub = p[masker.fillna(False)]
        if sub.empty:
            continue
        g = (sub.groupby(["week", "off", "receiver_player_name"])
                .agg(targets=("play_id", "size"), rec=("rec", "sum"),
                     yards=("yards_gained", "sum")).reset_index())
        for r in g.itertuples(index=False):
            volledig = idx.get((r.off, abbrev_key(r.receiver_player_name)))
            if not volledig:
                mis.add((r.off, r.receiver_player_name))
                continue
            play_context.append({
                "season": season, "week": int(r.week), "team": r.off,
                "player": volledig, "context": naam_ctx,
                "targets": int(r.targets), "rec": int(r.rec), "yards": float(r.yards),
            })
    print(f"  speelsituaties: {len(play_context)} regels"
          + (f", {len(mis)} namen niet herkend" if mis else ""))

    # ---- wat staat een verdediging toe, per positiegroep ----
    pos_van = {}
    for r in stats:
        pos_van.setdefault((r["team"], norm_name(r["player"])), r["pos"])

    def pos_of(team, naam):
        volledig = idx.get((team, abbrev_key(naam)))
        return pos_van.get((team, norm_name(volledig))) if volledig else None

    td = defaultdict(lambda: {"targets": 0, "rec": 0, "rec_yds": 0.0, "rush_yds": 0.0})
    for r in p.itertuples(index=False):
        pos = pos_of(r.off, r.receiver_player_name)
        if not pos:
            continue
        o = td[(int(r.week), r.dteam, pos)]
        o["targets"] += 1
        o["rec"] += int(r.rec)
        o["rec_yds"] += float(r.yards_gained or 0)

    runs = m[(m.play_type == "run") & m.rusher_player_name.notna()]
    for r in runs.itertuples(index=False):
        pos = pos_of(r.off, r.rusher_player_name)
        if not pos:
            continue
        td[(int(r.week), r.dteam, pos)]["rush_yds"] += float(r.yards_gained or 0)

    team_def_weekly = [
        {"season": season, "week": w, "team": t, "vs_pos": pos,
         "targets": o["targets"], "rec": o["rec"],
         "rec_yds": o["rec_yds"], "rush_yds": o["rush_yds"]}
        for (w, t, pos), o in sorted(td.items()) if t
    ]
    print(f"  teamverdediging: {len(team_def_weekly)} regels")

    # ---- run defense (rush toegestaan + box count) ----
    rd = defaultdict(lambda: {"rb_carries": 0, "rb_rush_yds": 0.0,
                              "box_sum": 0.0, "box_n": 0})
    for r in runs.itertuples(index=False):
        o = rd[(int(r.week), r.dteam)]
        o["rb_carries"] += 1
        o["rb_rush_yds"] += float(r.yards_gained or 0)
        box = getattr(r, "n_defense_box", None)
        if box is not None and box == box and box > 0:
            o["box_sum"] += float(box)
            o["box_n"] += 1
    run_def_weekly = [
        {"season": season, "week": w, "team": t, **o}
        for (w, t), o in sorted(rd.items()) if t
    ]

    # ---- rushing per RB ----
    rb = defaultdict(lambda: {"carries": 0, "rush_yds": 0.0})
    for r in runs.itertuples(index=False):
        volledig = idx.get((r.off, abbrev_key(r.rusher_player_name)))
        if not volledig:
            continue
        o = rb[(int(r.week), r.off, volledig)]
        o["carries"] += 1
        o["rush_yds"] += float(r.yards_gained or 0)
    rb_rush_weekly = [
        {"season": season, "week": w, "team": t, "player": pl,
         "carries": o["carries"], "rush_yds": o["rush_yds"],
         # YBC/YAC/broken tackles zitten niet in play-by-play: alleen met
         # charting-data te vullen. Nul betekent hier "onbekend".
         "ybc": 0, "yac": 0, "broken": 0}
        for (w, t, pl), o in sorted(rb.items())
    ]

    # ---- formatie: shotgun vs under center per team ----
    fpr = defaultdict(lambda: {"total": 0, "SHOTGUN": 0, "UNDER_CENTER": 0})
    for r in m[m.play_type.isin(["pass", "run"])].itertuples(index=False):
        if not r.off:
            continue
        o = fpr[(int(r.week), r.off)]
        o["total"] += 1
        o["SHOTGUN" if r.shotgun == 1 else "UNDER_CENTER"] += 1
    form_profile_weekly = [
        {"season": season, "week": w, "team": t, "form_total": o["total"], **o}
        for (w, t), o in sorted(fpr.items())
    ]

    return {
        "play_context": play_context,
        "team_def_weekly": team_def_weekly,
        "run_def_weekly": run_def_weekly,
        "rb_rush_weekly": rb_rush_weekly,
        "form_profile_weekly": form_profile_weekly,
    }


# ====================================================================
#  Odds ophalen  (the-odds-api.com)
# ====================================================================

def api_get(path, params):
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY ontbreekt. Zet hem als omgevingsvariabele.")
    params = dict(params or {})
    params["apiKey"] = key
    url = f"{API_BASE}{path}?{urlencode(params)}"
    for poging in range(4):
        try:
            with urlopen(url, timeout=30) as r:
                remaining = r.headers.get("x-requests-remaining")
                if remaining is not None:
                    print(f"    [quota over: {remaining}]", file=sys.stderr)
                return json.loads(r.read().decode())
        except HTTPError as e:
            if e.code == 429:                 # rate limit: even wachten
                time.sleep(5 * (poging + 1))
                continue
            if e.code == 422:                 # markt niet beschikbaar voor dit event
                return None
            raise
        except URLError:
            time.sleep(3 * (poging + 1))
    raise RuntimeError(f"Kon niet ophalen na meerdere pogingen: {path}")


def fetch_events():
    """Aankomende NFL-wedstrijden met hun event-id."""
    return api_get(f"/sports/{SPORT}/events", {}) or []


def fetch_props(event_id):
    """Player props voor een wedstrijd. Props zitten per event, niet per sport."""
    return api_get(
        f"/sports/{SPORT}/events/{event_id}/odds",
        {
            "regions": "us",
            "markets": ",".join(MARKETS),
            "oddsFormat": "american",
            "bookmakers": BOOKMAKER,
        },
    )


# ====================================================================
#  Odds afwikkelen  (closing line kiezen + Over/Under/Push bepalen)
# ====================================================================

def pick_closing(rows):
    """Per (speler, markt, week) de laatste opname voor kickoff."""
    best = {}
    for r in rows:
        key = (r["week"], norm_name(r["player"]), r["market"])
        ct = r.get("commence_time")
        cap = r.get("captured_at")
        if not cap:
            continue
        # opnames na kickoff negeren: die kende je niet toen je koos
        if ct:
            try:
                t_cap = datetime.strptime(cap, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                t_kick = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if t_cap > t_kick:
                    continue
            except ValueError:
                pass
        prev = best.get(key)
        if prev is None or cap > prev["captured_at"]:
            best[key] = r
    return best


def settle(line, actual):
    """Bepaalt Over / Under / Push."""
    if line is None or actual is None:
        return None
    if abs(float(actual) - float(line)) < 1e-9:
        return "Push"
    return "Over" if float(actual) > float(line) else "Under"


def load_aliases(path):
    """Handmatige uitzonderingen: {"odds-naam": "statistiek-naam"}.

    Voor gevallen die normalisatie niet vangt, zoals roepnamen
    (Hollywood Brown / Marquise Brown) of afwijkende spellingen.
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    return {norm_name(k): norm_name(v) for k, v in raw.items()}


def settle_odds(season, stats, roster, raw_odds_dir, aliases, allow_unmatched):
    """Leest alle odds-snapshots, kiest closing lines en wikkelt ze af.

    Retourneert de lijst voor data["odds"], of None als er geen snapshots zijn.
    """
    rawdir = Path(raw_odds_dir)
    snapshots = sorted(rawdir.glob(f"odds_{season}_*.json")) if rawdir.exists() else []
    if not snapshots:
        return None

    def key_of(name):
        n = norm_name(name)
        return aliases.get(n, n)

    stat_index = {}
    for r in stats:
        stat_index[(int(r["week"]), key_of(r["player"]))] = r
    roster_team = {key_of(r["player"]): r["team"] for r in roster}

    raw_rows = []
    for fp in snapshots:
        raw_rows.extend(json.loads(fp.read_text()))
    print(f"odds-opnames ingelezen: {len(raw_rows)} uit {len(snapshots)} snapshots")

    closing = pick_closing(raw_rows)
    print(f"closing lines: {len(closing)}")

    odds_out, unmatched = [], set()
    for (week, pkey, market), r in sorted(closing.items()):
        pkey = aliases.get(pkey, pkey)
        srow = stat_index.get((week, pkey))
        if srow is None:
            # Geen statistiek: of de wedstrijd is nog niet gespeeld, of de naam
            # koppelt niet. Dat onderscheid maken we via rooster/kickoff.
            unmatched.add((week, r["player"]))
            team, actual = roster_team.get(pkey), None
        else:
            team = srow.get("team") or roster_team.get(pkey)
            statfield = STAT_FOR_MARKET.get(market)
            actual = srow.get(statfield) if statfield else None

        odds_out.append({
            "season": season, "week": week, "team": team, "player": r["player"],
            "market": market, "line": r.get("line"),
            "price_over": r.get("price_over"), "price_under": r.get("price_under"),
            "actual": actual, "result": settle(r.get("line"), actual),
            "bookmaker": r.get("bookmaker"), "captured_at": r.get("captured_at"),
        })

    if closing:
        # Een week zonder ENIGE statistiek is simpelweg nog niet gespeeld.
        # Alleen namen in gespeelde weken tellen als echte koppelfout.
        gespeeld = {w for (w, _p) in stat_index.keys()}
        echt_mis = {(w, p) for (w, p) in unmatched if w in gespeeld}
        nog_niet = len(unmatched) - len(echt_mis)
        noemer = sum(1 for (w, _p, _m) in closing.keys() if w in gespeeld)
        if nog_niet:
            print(f"nog niet gespeeld: {nog_niet} regels (geen uitkomst, normaal)")
        pct = (100.0 * len(echt_mis) / noemer) if noemer else 0.0
        print(f"niet gekoppeld in gespeelde weken: {len(echt_mis)} ({pct:.1f}%)")
        for w, p in sorted(echt_mis)[:15]:
            print(f"    wk{w}: {p}")
        if len(echt_mis) > 15:
            print(f"    ... en nog {len(echt_mis)-15}")
        if pct > MAX_UNMATCHED_PCT and not allow_unmatched:
            sys.exit(
                f"\nGESTOPT: {pct:.1f}% van de namen koppelt niet "
                f"(grens {MAX_UNMATCHED_PCT}%).\n"
                f"Zet de uitzonderingen in je aliases-bestand, bijvoorbeeld:\n"
                f'  {{"Hollywood Brown": "Marquise Brown"}}\n'
                f"Of draai met --allow-unmatched als dit klopt."
            )

    afgewikkeld = sum(1 for o in odds_out if o["result"])
    print(f"odds afgewikkeld: {afgewikkeld} van {len(odds_out)}")
    return odds_out


# ====================================================================
#  Commando: odds
# ====================================================================

def cmd_odds(args):
    season = args.season or datetime.now(timezone.utc).year
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print(f"Ophalen: seizoen {season}, week {args.week}, boek {BOOKMAKER}")
    events = fetch_events()
    print(f"  {len(events)} wedstrijden gevonden")

    rijen = []
    for ev in events:
        props = fetch_props(ev["id"])
        if not props:
            continue
        for bm in props.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] not in MARKETS:
                    continue
                # Over en Under komen als aparte outcomes; koppel ze per speler.
                per_speler = {}
                for oc in mkt.get("outcomes", []):
                    speler = oc.get("description") or oc.get("name")
                    d = per_speler.setdefault(speler, {})
                    if oc.get("name") == "Over":
                        d["price_over"] = oc.get("price")
                        d["line"] = oc.get("point")
                    elif oc.get("name") == "Under":
                        d["price_under"] = oc.get("price")
                        d["line"] = oc.get("point")
                for speler, d in per_speler.items():
                    if "price_over" not in d or "price_under" not in d:
                        continue                 # eenzijdige line: onbruikbaar
                    rijen.append({
                        "season": season, "week": args.week, "player": speler,
                        "market": mkt["key"], "line": d.get("line"),
                        "price_over": d.get("price_over"),
                        "price_under": d.get("price_under"),
                        "bookmaker": bm.get("key"),
                        "home_team": ev.get("home_team"),
                        "away_team": ev.get("away_team"),
                        "commence_time": ev.get("commence_time"),
                        "captured_at": stamp,
                        # team en result vult 'build' later in
                        "team": None, "result": None, "actual": None,
                    })

    print(f"  {len(rijen)} prop-regels")
    if args.dry_run:
        print(json.dumps(rijen[:3], indent=2))
        return

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / f"odds_{season}_wk{args.week:02d}_{stamp}.json"
    out.write_text(json.dumps(rijen, indent=1))
    print(f"  weggeschreven: {out}")


# ====================================================================
#  Commando: build
# ====================================================================

def cmd_build(args):
    s = args.season
    print(f"=== seizoen {s} ===")
    roster = bouw_roster(s)
    schedule = bouw_schedule(s)
    stats = bouw_stats(s)
    afgeleid = bouw_uit_pbp(s, stats, roster)

    # Teamcodes van beide bronnen moeten overeenkomen, anders koppelen spelers
    # stil niet (bijv. roster 'AZ' vs statistieken 'ARI').
    if roster and stats:
        r_teams = {r["team"] for r in roster if r.get("team")}
        s_teams = {r["team"] for r in stats if r.get("team")}
        alleen_r, alleen_s = sorted(r_teams - s_teams), sorted(s_teams - r_teams)
        if alleen_r or alleen_s:
            print(f"LET OP: teamcodes wijken af. Alleen in roster: {alleen_r} | "
                  f"alleen in statistieken: {alleen_s}", file=sys.stderr)
            print("        Vul TEAM_FIX aan.", file=sys.stderr)

    data = {
        "season": s,
        "teams": sorted({r["team"] for r in stats if r.get("team")} |
                        {r["team"] for r in roster if r.get("team")}),
        "weeks": sorted({r["week"] for r in stats} |
                        {g["week"] for g in schedule}),
        "schedule": schedule,
        "player_weekly": stats,
        "roster": roster,
        "odds": [],
        "injuries": [],
        "vacated": [],
        "player_cov_weekly": [],
        "def_profile_weekly": [],
        "def_pos_weekly": [],
        "form_prod_weekly": [],
    }
    data.update(afgeleid)

    # tabellen die dit script niet kan maken overnemen uit het oude bestand
    if args.keep_from:
        print(f"overnemen uit {args.keep_from}...")
        oud = lees_bestaand(args.keep_from)
        for k in NIET_AFLEIDBAAR:
            if oud.get(k):
                data[k] = oud[k]
                print(f"  {k}: {len(oud[k])} regels behouden")

    # ---- odds ----
    if args.odds_json:
        po = Path(args.odds_json)
        if po.exists():
            data["odds"] = json.loads(po.read_text())
            print(f"odds (kant-en-klaar): {len(data['odds'])} regels")
        else:
            print(f"LET OP: {po} niet gevonden, odds overgeslagen", file=sys.stderr)
    else:
        aliases = load_aliases(args.aliases)
        settled = settle_odds(s, stats, roster, args.raw_odds, aliases,
                              args.allow_unmatched)
        if settled is not None:
            data["odds"] = settled
        elif data["odds"]:
            print(f"geen nieuwe odds-snapshots; {len(data['odds'])} regels "
                  f"behouden uit --keep-from")
        else:
            print("geen odds-snapshots gevonden (data/raw_odds leeg?)")

    schrijf_seizoen(args.out, s, data, backup=not args.no_backup)
    out = Path(args.out)
    print(f"\ngeschreven: {out} ({out.stat().st_size/1024:.0f} KB)")
    print(f"  teams {len(data['teams'])} | weken {len(data['weeks'])} | "
          f"odds {len(data['odds'])} "
          f"| afgewikkeld {sum(1 for o in data['odds'] if o.get('result'))}")
    leeg = [k for k, v in data.items() if isinstance(v, list) and not v]
    if leeg:
        print(f"  nog leeg: {', '.join(leeg)}")
        if not args.keep_from:
            print("  (coverage-tabellen? gebruik --keep-from je-oude-bestand.js)")
    print("\nKlaar. Ververs het dashboard met Ctrl+Shift+R.")


# ====================================================================
#  Commando: context
# ====================================================================

def cmd_context(args):
    path = Path(args.data)
    if not path.exists():
        sys.exit(f"Niet gevonden: {path}")
    kop, prefix, seizoen, data, staart = split_season_file(path.read_text())
    if args.season and args.season != seizoen:
        sys.exit(f"Bestand hoort bij seizoen {seizoen}, niet {args.season}.")

    print(f"seizoen {seizoen} ingelezen: "
          f"{len(data.get('player_weekly', []))} statistiekregels, "
          f"{len(data.get('odds', []))} odds")

    stats = data.get("player_weekly", [])
    roster = data.get("roster", [])
    if not stats and not roster:
        sys.exit("Bestand bevat geen player_weekly of roster om namen mee te "
                 "koppelen.")

    afgeleid = bouw_uit_pbp(seizoen, stats, roster)
    pc = afgeleid["play_context"]
    if not pc:
        sys.exit("Niets gekoppeld -- controleer of play-by-play/FTN voor dit "
                 "seizoen al bestaat.")
    data["play_context"] = pc

    if not args.no_backup:
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy(path, bak)
        print(f"back-up: {bak}")

    path.write_text(kop + prefix + json.dumps(data, separators=(",", ":")) + staart)
    print(f"bijgewerkt: {path} ({path.stat().st_size/1024:.0f} KB, "
          f"{len(pc)} speelsituatie-regels)")
    print("\nKlaar. Ververs het dashboard met Ctrl+Shift+R.")


# ====================================================================
#  Commando: weekly  (voor de wekelijkse geplande taak)
# ====================================================================

def huidige_week():
    """Bepaalt (seizoen, eerstvolgende niet-gespeelde reguliere week) uit het
    speelschema van nflverse."""
    with urlopen(SCHEDULE_URL, timeout=120) as r:
        rows = lees_csv(r.read())
    reg = [g for g in rows if (g.get("game_type") or "REG").upper() == "REG"]
    if not reg:
        sys.exit("Geen reguliere wedstrijden in het speelschema gevonden.")

    def gespeeld(g):
        return bool((g.get("result") or "").strip() or (g.get("home_score") or "").strip())

    ongespeeld = [g for g in reg if not gespeeld(g)]
    if ongespeeld:
        season = max(int(g["season"]) for g in ongespeeld)
        week = min(int(g["week"]) for g in ongespeeld if int(g["season"]) == season)
    else:
        season = max(int(g["season"]) for g in reg)
        week = max(int(g["week"]) for g in reg if int(g["season"]) == season)
    return season, week


def cmd_weekly(args):
    # --dir: waar de dashboard_data_<jaar>.js en data/ staan. Standaard naast dit
    # script (laptop); de cloud-workflow zet dit op de repo-root.
    here = Path(args.dir).resolve() if getattr(args, "dir", None) else Path(__file__).resolve().parent
    os.chdir(here)                       # data/raw_odds en dashboard_data_*.js zijn relatief
    logpad = here / "weekly_update.log"

    def note(msg):
        regel = f"[{datetime.now():%Y-%m-%d %H:%M}] " + " ".join(str(msg).split())
        print(regel)
        with logpad.open("a", encoding="utf-8") as fh:
            fh.write(regel + "\n")

    note("=== wekelijkse update gestart ===")
    try:
        season, week = huidige_week()
    except SystemExit:
        raise
    except Exception as e:
        note(f"FOUT bij weekbepaling: {e}")
        sys.exit(1)
    note(f"seizoen {season}, eerstvolgende week {week}")

    # 1) odds-snapshot voor de aankomende wedstrijden (alleen met API-sleutel)
    if os.environ.get("ODDS_API_KEY"):
        try:
            cmd_odds(argparse.Namespace(season=season, week=week, dry_run=False))
            note(f"odds-snapshot week {week} opgehaald")
        except SystemExit as e:
            note(f"odds overgeslagen ({e})")
        except Exception as e:
            note(f"odds mislukt: {e}")
    else:
        note("ODDS_API_KEY niet gezet -> odds-snapshot overgeslagen")

    # 2) seizoensbestand herbouwen: verse statistieken + opgeslagen odds afwikkelen
    out = here / f"dashboard_data_{season}.js"
    try:
        cmd_build(argparse.Namespace(
            season=season, out=str(out),
            keep_from=str(out) if out.exists() else None,
            raw_odds=str(RAW_DIR), odds_json=None,
            aliases="data/name_aliases.json",
            allow_unmatched=True,           # geplande taak mag niet hard stoppen
            no_backup=False,
        ))
        note(f"{out.name} herbouwd ({out.stat().st_size/1024:.0f} KB)")
    except SystemExit as e:
        boodschap = " ".join(str(e).split())
        # Vóór week 1 bestaan de statistieken nog niet -- geen fout, niks te doen.
        if "nog niet begonnen" in boodschap or "Niet gevonden" in boodschap:
            note(f"nog geen data voor week {week} ({boodschap}) -- niets te herbouwen")
            note("=== klaar (geen update nodig) ===")
            return
        note(f"build gestopt ({boodschap})")
        sys.exit(1)
    except Exception as e:
        note(f"build mislukt: {e}")
        sys.exit(1)

    note("=== klaar; ververs het dashboard met Ctrl+Shift+R ===")


# ====================================================================
#  main
# ====================================================================

def main():
    ap = argparse.ArgumentParser(
        description="NFL-dashboard pipeline: odds ophalen, seizoen bouwen, "
                    "speelsituaties bijwerken.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("odds", help="player-prop odds-snapshot ophalen")
    po.add_argument("--week", type=int, required=True, help="NFL-week (1-18)")
    po.add_argument("--season", type=int, default=None,
                    help="seizoensjaar, standaard huidig")
    po.add_argument("--dry-run", action="store_true", help="niets wegschrijven")
    po.set_defaults(func=cmd_odds)

    pb = sub.add_parser("build", help="volledig dashboard_data_<jaar>.js bouwen")
    pb.add_argument("--season", type=int, required=True)
    pb.add_argument("--out", required=True)
    pb.add_argument("--keep-from", default=None,
                    help="bestaand .js-bestand; neemt coverage-/injury-tabellen daaruit over")
    pb.add_argument("--raw-odds", default=str(RAW_DIR),
                    help=f"map met odds-snapshots (standaard {RAW_DIR})")
    pb.add_argument("--odds-json", "--odds", default=None, dest="odds_json",
                    help="kant-en-klare odds-lijst als json; slaat afwikkelen over")
    pb.add_argument("--aliases", default="data/name_aliases.json",
                    help="{\"odds-naam\": \"statistiek-naam\"} voor namen die niet matchen")
    pb.add_argument("--allow-unmatched", action="store_true",
                    help="doorgaan ondanks te veel niet-gekoppelde odds-namen")
    pb.add_argument("--no-backup", action="store_true")
    pb.set_defaults(func=cmd_build)

    pc = sub.add_parser("context",
                        help="alleen play_context bijwerken in een bestaand bestand")
    pc.add_argument("--data", required=True, help="bestaand dashboard_data_<jaar>.js")
    pc.add_argument("--season", type=int, default=None,
                    help="controle: moet overeenkomen met het bestand")
    pc.add_argument("--no-backup", action="store_true")
    pc.set_defaults(func=cmd_context)

    pw = sub.add_parser("weekly",
                        help="wekelijkse update: odds-snapshot + seizoensbestand herbouwen "
                             "(seizoen en week worden automatisch bepaald)")
    pw.add_argument("--dir", default=None,
                    help="map met de dashboard_data_<jaar>.js en data/ (standaard: naast dit script)")
    pw.set_defaults(func=cmd_weekly)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
