#!/usr/bin/env python3
"""Create one CKAN dataset per day over a date range for the
``pnaig_weather_radar`` schema.

Standalone helper script (not part of the docker deployment): it talks to a
CKAN instance over HTTP through the ckanapi library and calls ``package_create``
once per day in the requested range, assuming the collection integrates data
daily (one dataset per day, as with ``sophy-ppi-products-20250601``).

Per-day fields are derived automatically:

* ``name``  / ``title``     from the templates (see ``--name-template``)
* ``start_date``            ``YYYY-MM-DDT00:00:00``
* ``end_date``              ``YYYY-MM-DDT23:59:59``

Every other schema field comes from the defaults below, from a JSON template
file (``--template``) and/or from repeated ``--field NAME=VALUE`` options.

Example:

    .venv/bin/python bin/create_datasets.py \\
        --ckan-url http://localhost:5000 \\
        --api-token "$CKAN_API_TOKEN" \\
        --owner-org datos-de-radar-meteorologico \\
        --start-date 2025-06-01 --end-date 2025-06-30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

try:
    from ckanapi import (
        CKANAPIError,
        NotAuthorized,
        NotFound,
        RemoteCKAN,
        ValidationError,
    )
except ImportError:  # pragma: no cover - dependency guard
    sys.exit(
        "ckanapi is not installed. Install it first, e.g.:\n"
        "    .venv/bin/python -m pip install ckanapi"
    )

USER_AGENT = "pnaig-create-datasets/1.0 (+https://www.gob.pe/igp)"
DATASET_TYPE = "pnaig_weather_radar"

MONTHS_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)

DEFAULT_NAME_TEMPLATE = "sophy-ppi-products-{date_compact}"
DEFAULT_TITLE_TEMPLATE = (
    "Productos de radar {instrument_name} - {scanning_mode} - {date_es}"
)
DEFAULT_NOTES_TEMPLATE = (
    "Productos del radar {instrument_name} ({date_es})"
)

# Fields shared by every dataset of the collection. Override with --template
# and/or --field NAME=VALUE.
DEFAULT_FIELDS: dict = {
    "instrument_type": "Radar Meteorologico",
    "instrument_name": "SOPHy",
    "instrument_id": "WR-SOP",
    "latitude": "-12.040436",
    "longitude": "-75.295893",
    "altitude": "3379.2147",
    "scanning_mode": "PPI",
    "license_id": "cc-by",
    "url": "http://pnaig.igp.gob.pe/sophy",
    "version": "1.0",
    "author": "Yamina Silva",
    "author_email": "fsilva@igp.gob.pe",
    "maintainer": "Carlos del Castillo",
    "maintainer_email": "cdelcastillo@igp.gob.pe",
    "tag_string": "Radar Meteorologico,SOPHy",
    "measured_variables": [
        "Reflectividad",
        "Ancho espectral",
        "Velocidad Doppler",
    ],
    "private": False,
}

# Schema fields declared with `not_empty` that must be present before sending.
REQUIRED_FIELDS = (
    "title",
    "name",
    "start_date",
    "end_date",
    "instrument_type",
    "instrument_name",
    "instrument_id",
    "latitude",
    "longitude",
    "altitude",
    "scanning_mode",
    "owner_org",
)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date '{value}', expected YYYY-MM-DD"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create one pnaig_weather_radar dataset per day over a date range."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--ckan-url",
        default=os.environ.get("CKAN_URL"),
        help="Base URL of the CKAN instance (env: CKAN_URL).",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("CKAN_API_TOKEN"),
        help="CKAN API token (env: CKAN_API_TOKEN).",
    )
    parser.add_argument(
        "--start-date",
        required=True,
        type=parse_date,
        help="First day of the range (YYYY-MM-DD), inclusive.",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        type=parse_date,
        help="Last day of the range (YYYY-MM-DD), inclusive.",
    )
    parser.add_argument(
        "--owner-org",
        help="id or name of the owning organization.",
    )
    parser.add_argument(
        "--dataset-type",
        default=DATASET_TYPE,
        help="Scheming dataset type to create.",
    )
    parser.add_argument(
        "--template",
        type=Path,
        help="JSON file with the fields shared by every dataset. Merged on "
        "top of the built-in defaults.",
    )
    parser.add_argument(
        "--field",
        metavar="NAME=VALUE",
        action="append",
        default=[],
        help="Override a single shared field (repeatable). VALUE is parsed as "
        "JSON when possible, otherwise kept as a string.",
    )
    parser.add_argument(
        "--name-template",
        default=DEFAULT_NAME_TEMPLATE,
        help="Template for the dataset URL slug.",
    )
    parser.add_argument(
        "--title-template",
        default=DEFAULT_TITLE_TEMPLATE,
        help="Template for the dataset title.",
    )
    parser.add_argument(
        "--notes-template",
        default=DEFAULT_NOTES_TEMPLATE,
        help="Template for the dataset description.",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="Create the datasets as private.",
    )
    parser.add_argument(
        "--on-existing",
        choices=("skip", "fail"),
        default="skip",
        help="What to do when a dataset with the same name already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the payloads that would be sent without creating anything.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Do not verify the TLS certificate of the CKAN instance.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-request timeout in seconds.",
    )

    args = parser.parse_args(argv)

    if not args.ckan_url:
        parser.error("--ckan-url is required (or set CKAN_URL).")
    if not args.api_token:
        parser.error("--api-token is required (or set CKAN_API_TOKEN).")
    if args.end_date < args.start_date:
        parser.error("--end-date must not be earlier than --start-date.")

    return args


def load_shared_fields(args: argparse.Namespace) -> dict:
    """Merge built-in defaults, the JSON template and --field overrides."""
    fields = dict(DEFAULT_FIELDS)

    if args.template:
        try:
            loaded = json.loads(args.template.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Could not read --template {args.template}: {exc}")
        if not isinstance(loaded, dict):
            raise SystemExit(f"--template {args.template} must contain a JSON object.")
        fields.update(loaded)

    for item in args.field:
        if "=" not in item:
            raise SystemExit(f"--field must use NAME=VALUE, got: {item}")
        key, _, raw = item.partition("=")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        fields[key.strip()] = value

    if args.owner_org:
        fields["owner_org"] = args.owner_org
    if args.private:
        fields["private"] = True

    return fields


def date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def format_date_es(day: date) -> str:
    return f"{day.day:02d} de {MONTHS_ES[day.month - 1]} de {day.year}"


def render_templates(day: date, shared: dict, args: argparse.Namespace) -> dict:
    """Build the per-day package_create payload."""
    placeholders = {
        key: value
        for key, value in shared.items()
        if isinstance(value, (str, int, float))
    }
    placeholders.update(
        {
            "date": day.isoformat(),
            "date_compact": day.strftime("%Y%m%d"),
            "date_es": format_date_es(day),
            "year": f"{day.year:04d}",
            "month": f"{day.month:02d}",
            "day": f"{day.day:02d}",
        }
    )

    def render(template: str, label: str) -> str:
        try:
            return template.format(**placeholders)
        except KeyError as exc:
            raise SystemExit(
                f"Unknown placeholder {exc} in {label}. Available: "
                + ", ".join(sorted(placeholders))
            )

    payload = dict(shared)
    payload.update(
        {
            "type": args.dataset_type,
            "name": render(args.name_template, "--name-template").lower(),
            "title": render(args.title_template, "--title-template"),
            "notes": render(args.notes_template, "--notes-template"),
            "start_date": f"{day.isoformat()}T00:00:00",
            "end_date": f"{day.isoformat()}T23:59:59",
        }
    )
    return payload


def validate_payload(payload: dict, day: date) -> None:
    missing = [
        field
        for field in REQUIRED_FIELDS
        if payload.get(field) in (None, "", [])
    ]
    if missing:
        raise SystemExit(
            f"Missing required field(s) for {day.isoformat()}: "
            + ", ".join(missing)
            + "\nProvide them with --owner-org, --template or --field NAME=VALUE."
        )


def dataset_exists(
    ckan: RemoteCKAN, name: str, requests_kwargs: dict
) -> bool:
    """Return True when a dataset with this name/id already exists.

    ``package_create`` cannot be trusted to reject duplicates: on some CKAN
    setups it silently updates the existing dataset, which would overwrite
    curated metadata. Always check first.
    """
    try:
        ckan.call_action(
            "package_show", {"id": name}, requests_kwargs=requests_kwargs
        )
    except NotFound:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shared = load_shared_fields(args)

    days = list(date_range(args.start_date, args.end_date))
    payloads = []
    for day in days:
        payload = render_templates(day, shared, args)
        validate_payload(payload, day)
        payloads.append((day, payload))

    names = [payload["name"] for _, payload in payloads]
    if len(set(names)) != len(names):
        raise SystemExit(
            "--name-template produces duplicate dataset names; include a date "
            "placeholder such as {date_compact}."
        )

    requests_kwargs: dict = {"timeout": args.timeout}
    if args.insecure:
        requests_kwargs["verify"] = False

    ckan = RemoteCKAN(args.ckan_url, apikey=args.api_token, user_agent=USER_AGENT)

    print(
        f"{len(payloads)} dataset(s) of type '{args.dataset_type}' "
        f"from {args.start_date.isoformat()} to {args.end_date.isoformat()}."
    )

    created = skipped = failed = 0

    for day, payload in payloads:
        name = payload["name"]

        if args.dry_run:
            print(f"  create   {name} [dry-run]")
            print(
                "           "
                + json.dumps(payload, ensure_ascii=False, sort_keys=True)
            )
            continue

        try:
            exists = dataset_exists(ckan, name, requests_kwargs)
        except (
            NotAuthorized,
            CKANAPIError,
            requests.exceptions.RequestException,
        ) as exc:
            print(f"  FAILED   {name}: {exc}", file=sys.stderr)
            failed += 1
            continue

        if exists:
            if args.on_existing == "skip":
                print(f"  skip     {name} (already exists)")
                skipped += 1
                continue
            print(
                f"  FAILED   {name}: a dataset with this name already exists",
                file=sys.stderr,
            )
            failed += 1
            continue

        try:
            result = ckan.call_action(
                "package_create", payload, requests_kwargs=requests_kwargs
            )
        except ValidationError as exc:
            print(f"  FAILED   {name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        except (
            NotAuthorized,
            NotFound,
            CKANAPIError,
            requests.exceptions.RequestException,
        ) as exc:
            print(f"  FAILED   {name}: {exc}", file=sys.stderr)
            failed += 1
            continue

        created += 1
        print(f"  create   {name} -> {result['id']}")

    if args.dry_run:
        print(f"\nDry run: {len(payloads)} dataset(s) rendered, nothing created.")
        return 0

    print(f"\nDone: {created} created, {skipped} skipped, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
