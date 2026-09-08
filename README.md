# NFL Matchup Terminal — website

De online versie van het dashboard. Zelfde `index.html` als op de laptop; de
data wordt **elke donderdag automatisch** ververst door GitHub Actions en de site
draait op Cloudflare Pages achter een wachtwoord.

```
GitHub repo
  ├─ index.html + dashboard_data_<jaar>.js + logos/     ← de site
  ├─ pipeline/nfl_pipeline.py                            ← de datapijplijn
  ├─ functions/_middleware.js                            ← wachtwoord-slot (Cloudflare)
  └─ .github/workflows/weekly.yml                        ← donderdag-cron

  elke donderdag 08:00 UTC:
    Actions → nfl_pipeline.py weekly --dir .
            → commit dashboard_data_<jaar>.js (+ odds-snapshots)
            → push  → Cloudflare Pages deployt automatisch opnieuw
```

De laptop hoeft nergens voor aan te staan.

---

## Eenmalige setup

### 1. GitHub

1. Maak deze repo aan op github.com (mag **privé**) en push hem (zie de push-
   commando's die je bij het aanmaken kreeg, of de instructies onderaan).
2. **Settings → Secrets and variables → Actions → New repository secret**
   - Naam: `ODDS_API_KEY`
   - Waarde: je sleutel van the-odds-api.com
3. **Actions-tab** → als Actions uit staat, zet 'm aan. Klik daarna bij
   *"Weekly dashboard update"* op **Run workflow** om het meteen te testen.

### 2. Cloudflare Pages (hosting + wachtwoord)

1. Maak een gratis account op dash.cloudflare.com.
2. **Workers & Pages → Create → Pages → Connect to Git** → kies deze repo.
3. Build-instellingen:
   - Framework preset: **None**
   - Build command: **leeg laten**
   - Build output directory: **`/`**
4. **Save and Deploy**. Je krijgt een URL als `https://<project>.pages.dev`.
5. **Settings → Environment variables** → voeg toe (voor **Production én Preview**):
   - Naam: `SITE_PASSWORD`
   - Waarde: het wachtwoord dat je wilt delen
6. **Deployments → Retry deployment** (of wacht op de volgende push) zodat de
   variabele actief wordt.

Klaar. De URL vraagt nu om een wachtwoord (gebruikersnaam mag je leeg laten of
een willekeurig woord invullen). Die URL + wachtwoord stuur je door naar wie je
wilt.

---

## Handmatig verversen

Zonder tot donderdag te wachten: **Actions-tab → Weekly dashboard update →
Run workflow**. Of lokaal:

```
python pipeline/nfl_pipeline.py weekly --dir .
git add -A dashboard_data_*.js data/ && git commit -m "update" && git push
```

## Wachtwoord wijzigen

Cloudflare Pages → Settings → Environment variables → `SITE_PASSWORD` aanpassen →
opnieuw deployen.

## Wat de wekelijkse run doet

- Bepaalt seizoen + eerstvolgende week uit het nflverse-speelschema
- Haalt een player-props odds-snapshot (reception yds / receptions / rush yds,
  DraftKings) voor die week op — snapshots worden nooit overschreven
- Herbouwt `dashboard_data_<seizoen>.js`: spelersstatistieken, play-by-play-
  splitsingen, verdedigingstabellen, verse injury reports uit nflverse, en
  wikkelt alle odds-snapshots af tot closing lines met Over/Under/Push
- De injury-tabel wordt daarnaast vr/za/zo apart ververst (`injuries.yml`),
  want de officiele NFL game-status komt er pas na donderdag bij

**Coverage- en defensie-profiel-panelen** hebben charting-data nodig die de
pijplijn niet zelf kan maken (zoals bij seizoen 2025 handmatig aangeleverd).
Zonder die bron blijven die twee panelen leeg; de rest vult vanzelf.

## Bestanden

| Pad | Wat |
|-----|-----|
| `index.html` | het dashboard (identiek aan de laptopversie) |
| `dashboard_data_<jaar>.js` | seizoensdata, wekelijks ververst |
| `logos/` | teamlogo's |
| `data/raw_odds/` | opgeslagen odds-snapshots (input voor het afwikkelen) |
| `pipeline/nfl_pipeline.py` | `odds` / `build` / `context` / `injuries` / `weekly` |
| `functions/_middleware.js` | wachtwoord-slot op Cloudflare |
| `.github/workflows/weekly.yml` | donderdag-cron (volledige herbouw) |
| `.github/workflows/injuries.yml` | vr/za/zo-cron (alleen de injury-tabel verversen) |
