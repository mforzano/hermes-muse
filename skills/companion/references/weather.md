# Weather context (off by default)

Read this file only when `preferences.weather_context` is `true` (context `preferences`; absent or `false` means off). While it is off, skip weather entirely: do not look up or infer a location, do not call any weather service, and do not write a weather source_check.

## Privacy

A location is private data. Enabling the preference sends the configured place to a third-party weather service on each patrol that needs a forecast. Muse itself never obtains, stores or forwards a location. Use only a place the user explicitly gave for weather (a city or coordinates recorded in the Coverage scope prose of PROACTIVE_PREFERENCES.md, or confirmed in conversation for an actual trip) or an authorized device-location tool you already have. With no such location, skip weather rather than guessing or asking repeatedly.

## Read recipe (Open-Meteo, public, no credentials)

`python3 -c "import urllib.request, json; print(json.dumps(json.loads(urllib.request.urlopen('https://api.open-meteo.com/v1/forecast?latitude=<lat>&longitude=<lon>&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,weather_code&timezone=auto&forecast_days=5').read().decode())['daily']))"`

Or a local weather script/CLI the user has configured. Output: daily min/max temperature, precipitation probability/sum and WMO weather codes. Query during `proactive-watch` when an event, trip or recurring routine falls in the next 1-5 days.

## When to notify

Notify only when precipitation probability >= 70% (or thunderstorm / heavy-rain codes), or a >= 5°C drop or rise against recent conditions, overlaps an upcoming calendar event, outing, trip or known routine (school run, commute). Give practical advice (umbrella, extra layers, an earlier or later departure), not a raw forecast. Record coverage with source_check: connection "weather", account the provider (e.g. "open-meteo"), resource "forecast:daily".
