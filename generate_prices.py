from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_URL = "https://api.energidataservice.dk/dataset/DayAheadPrices"
TIMEZONE = ZoneInfo("Europe/Copenhagen")
OUTPUT_FOLDER = Path("docs")

# Skift til DK2, hvis du bor øst for Storebælt.
PRICE_AREA = os.environ.get("PRICE_AREA", "DK1").upper()

# Spotpris inklusive dansk moms.
INCLUDE_VAT = True


def api_request(start_date: str, end_date: str) -> dict:
    """Hent priser fra Energi Data Service med genforsøg."""

    query = {
        "start": start_date,
        "end": end_date,
        "filter": json.dumps(
            {"PriceArea": [PRICE_AREA]},
            separators=(",", ":"),
        ),
        "sort": "TimeDK ASC",
        "limit": 1000,
    }

    url = f"{API_URL}?{urlencode(query)}"
    headers = {
        "User-Agent": "apple-watch-elpriser/1.0",
        "Accept": "application/json",
    }

    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            request = Request(url, headers=headers)

            with urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"API returnerede HTTP {response.status}"
                    )

                return json.loads(response.read().decode("utf-8"))

        except (HTTPError, URLError, TimeoutError, RuntimeError) as error:
            last_error = error

            if attempt < 3:
                time.sleep(attempt * 10)

    raise RuntimeError(
        f"Kunne ikke hente priser efter tre forsøg: {last_error}"
    )


def find_value(record: dict, possible_names: list[str]):
    """Find en værdi, selv hvis API-feltnavnet ændres lidt."""

    for name in possible_names:
        if name in record and record[name] is not None:
            return record[name]

    return None


def parse_datetime(record: dict) -> datetime:
    """Læs dansk eller UTC-tid fra en API-post."""

    local_value = find_value(
        record,
        ["TimeDK", "HourDK", "DeliveryStartDK"],
    )

    if local_value:
        parsed = datetime.fromisoformat(
            str(local_value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TIMEZONE)
        else:
            parsed = parsed.astimezone(TIMEZONE)

        return parsed

    utc_value = find_value(
        record,
        ["TimeUTC", "HourUTC", "DeliveryStartUTC"],
    )

    if utc_value:
        parsed = datetime.fromisoformat(
            str(utc_value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))

        return parsed.astimezone(TIMEZONE)

    raise ValueError(f"Intet genkendeligt tidsfelt i posten: {record}")


def parse_price_dkk_per_mwh(record: dict) -> float:
    """Læs prisen i DKK/MWh fra forskellige mulige feltnavne."""

    value = find_value(
        record,
        [
            "DayAheadPriceDKK",
            "SpotPriceDKK",
            "PriceDKK",
            "DKK_per_MWh",
        ],
    )

    if value is None:
        raise ValueError(f"Intet genkendeligt prisfelt i posten: {record}")

    return float(value)


def load_hourly_prices(records: list[dict]) -> dict[datetime, float]:
    """
    Saml eventuelle 15-minutters priser til én gennemsnitspris
    for hver klokktime.
    """

    quarter_prices: dict[datetime, list[float]] = defaultdict(list)

    for record in records:
        try:
            timestamp = parse_datetime(record)
            price = parse_price_dkk_per_mwh(record)
        except (ValueError, TypeError):
            continue

        hour = timestamp.replace(minute=0, second=0, microsecond=0)
        quarter_prices[hour].append(price)

    hourly_prices: dict[datetime, float] = {}

    for hour, prices in quarter_prices.items():
        hourly_prices[hour] = mean(prices)

    return hourly_prices


def format_price(price_dkk_per_mwh: float) -> str:
    """Konvertér DKK/MWh til kr./kWh."""

    price_dkk_per_kwh = price_dkk_per_mwh / 1000

    if INCLUDE_VAT:
        price_dkk_per_kwh *= 1.25

    return f"{price_dkk_per_kwh:.2f}".replace(".", ",")


def create_message(
    start_hour: datetime,
    hourly_prices: dict[datetime, float],
) -> str | None:
    """Lav teksten med de næste 12 klokketimer."""

    lines = []
    missing_hours = []

    for offset in range(12):
        hour = start_hour + timedelta(hours=offset)
        price = hourly_prices.get(hour)

        if price is None:
            missing_hours.append(hour)
            continue

        end_hour = hour + timedelta(hours=1)

        lines.append(
            f"{hour:%H}–{end_hour:%H}: "
            f"{format_price(price)} kr./kWh"
        )

    # Vi udgiver kun filen, hvis alle 12 timer findes.
    if missing_hours:
        return None

    vat_text = "inkl. moms" if INCLUDE_VAT else "ekskl. moms"

    header = (
        f"Elpris – næste 12 timer\n"
        f"{PRICE_AREA} · spotpris {vat_text}\n"
    )

    return header + "\n".join(lines)


def write_outputs(hourly_prices: dict[datetime, float]) -> None:
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

    # Fjern gamle timefiler, så forældede priser ikke bliver vist.
    for old_file in OUTPUT_FOLDER.glob("20??-??-??-??.txt"):
        old_file.unlink()

    if not hourly_prices:
        raise RuntimeError("Der blev ikke fundet nogen brugbare priser.")

    sorted_hours = sorted(hourly_prices)
    first_hour = sorted_hours[0]
    last_hour = sorted_hours[-1]

    generated_files = 0
    current_hour = first_hour

    while current_hour <= last_hour:
        message = create_message(current_hour, hourly_prices)

        if message:
            filename = current_hour.strftime("%Y-%m-%d-%H.txt")
            output_path = OUTPUT_FOLDER / filename
            output_path.write_text(message, encoding="utf-8")
            generated_files += 1

        current_hour += timedelta(hours=1)

    status = {
        "generated_at": datetime.now(TIMEZONE).isoformat(),
        "price_area": PRICE_AREA,
        "vat_included": INCLUDE_VAT,
        "hourly_prices_found": len(hourly_prices),
        "message_files_created": generated_files,
        "first_price": first_hour.isoformat(),
        "last_price": last_hour.isoformat(),
    }

    (OUTPUT_FOLDER / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    (OUTPUT_FOLDER / "index.html").write_text(
        """
<!doctype html>
<html lang="da">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width">
  <title>Apple Watch elpriser</title>
</head>
<body>
  <h1>Apple Watch elpriser</h1>
  <p>Prisfilerne bliver genereret automatisk med GitHub Actions.</p>
  <p>Se <a href="status.json">status.json</a> for seneste opdatering.</p>
</body>
</html>
""".strip(),
        encoding="utf-8",
    )

    print(f"Oprettede {generated_files} prisfiler.")


def main() -> None:
    if PRICE_AREA not in {"DK1", "DK2"}:
        raise ValueError("PRICE_AREA skal være DK1 eller DK2.")

    now = datetime.now(TIMEZONE)

    # Hent fra i går til to dage frem.
    # Det gør løsningen mere robust omkring midnat og sommertid.
    start_date = (now.date() - timedelta(days=1)).isoformat()
    end_date = (now.date() + timedelta(days=3)).isoformat()

    data = api_request(start_date, end_date)
    records = data.get("records", [])

    if not isinstance(records, list) or not records:
        raise RuntimeError(
            "API'et returnerede ingen poster. "
            "Kontrollér GitHub Actions-loggen."
        )

    hourly_prices = load_hourly_prices(records)
    write_outputs(hourly_prices)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FEJL: {error}", file=sys.stderr)
        sys.exit(1)
