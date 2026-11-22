# Lorenzen Bay Tide Planner

A static tide-prediction viewer for the Lorenzen Bay mooring in Raglan Harbour. The site displays a one-minute predicted sea-level curve, high and low tides, the current time, and periods when the predicted level is above the initial grounding threshold.

## Tide data and analysis

`Raglan_Wharf.txt` contains 614,770 minute timestamps from 1 July 2008 to 1 September 2009. The rows are continuous in time. There are 26,915 blank readings during one gap from 13 February to 4 March 2009, plus 15 readings equal to the repeated `2.01166e-7` sentinel; the analysis excludes those values. The file does not specify the time zone, units, or vertical datum; values are treated as metres.

The analysis uses Python UTide at Raglan's approximate latitude (`-37.8°`). Since the source file has no time zone metadata, the build compares UTC and fixed New Zealand Standard Time interpretations against the provided MetService high/low event times; it falls back to the reported falling tide at 11:12 on 22 November 2026 if those times do not distinguish them. The event-height values are not compared because the source file's vertical datum is unspecified. The grounding time sets the initial threshold to the model's predicted water level at that moment. This assumes the gauge and mooring can be treated as the same location. The threshold is an estimate, not a verified boat clearance.

The pipeline holds out the final 45 days of observations to report bias, mean absolute error, and RMSE before fitting the final model to all valid readings. Predictions are created at one-minute intervals for the next year and split into monthly files so the viewer only loads the selected dates.

## Run locally

Requirements: Python 3.10+ and Node.js 20+.

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
Set-Location web
npm ci
npm run dev
```

The development and production build scripts regenerate the forecast data under `web/public/data/`. The generated files are intentionally not committed.

## GitHub Pages

`.github/workflows/pages.yml` builds and deploys the site on pushes to `main`, and refreshes the rolling forecast daily. It also supports manual runs. Enable **Settings → Pages → Build and deployment → GitHub Actions** once in the repository settings. After that, a push to `main` publishes the site.
