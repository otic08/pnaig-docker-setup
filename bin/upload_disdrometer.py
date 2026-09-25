#!/usr/bin/env python3
"""Create one CKAN dataset per year of LAMAR disdrometer data and upload the
monthly NetCDF files as its resources.

Standalone helper script (not part of the docker deployment): it talks to a
CKAN instance over HTTP through the ckanapi library. For every ``YYYY``
sub-directory of ``--directory`` it:

1. derives the temporal coverage from the ``DIS_YYYYMM.nc`` file names,
2. calls ``package_create`` once for the year (existing datasets are reused
   as-is, their metadata is not modified),
3. uploads every ``*.nc`` file in the directory with ``resource_create``
   through the FileStore API.

Dataset metadata targets the ``pnaig_clima_dataset`` scheming schema; the
defaults below come from the DIS rows of ``produccion_catInstrumentos -
catalogo.csv`` (Disdrómetro OTT Parsivel² at HYO). Override anything with
``--template`` and/or repeated ``--field NAME=VALUE``.

Example:

    .venv/bin/python bin/upload_disdrometer.py \\
        --ckan-url http://localhost:5000 \\
        --api-token "$CKAN_API_TOKEN" \\
        --directory ~/Documents/LAMAR/DIS \\
        --owner-org datos-de-disdrometo
"""

from __future__ import annotations

import argparse
import calendar
import json
import mimetypes
import os
import re
import sys
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

USER_AGENT = "pnaig-upload-disdrometer/1.0 (+https://www.gob.pe/igp)"
DATASET_TYPE = "pnaig_clima_dataset"
DEFAULT_DIRECTORY = Path("/home/igp-otidgx/Documents/LAMAR/DIS")
DEFAULT_OWNER_ORG = "datos-de-disdrometo"
DEFAULT_INCLUDE = "*.nc"

MONTHS_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)

DEFAULT_NAME_TEMPLATE = "disdrometer-parsivel2-{year}"
DEFAULT_TITLE_TEMPLATE = (
    "Disdrómetro {instrument_brand} {instrument_model} - {year}"
)
DEFAULT_NOTES_TEMPLATE = (
    "Datos mensuales del disdrómetro {instrument_brand} {instrument_model} "
    "en {location} ({start_date} a {end_date})."
)
DEFAULT_RESOURCE_DESCRIPTION_TEMPLATE = (
    "Datos del disdrómetro {instrument_brand} {instrument_model} "
    "({month_es} de {year})"
)

FILE_MONTH_RE = re.compile(r"DIS_(\d{4})(\d{2})")
YEAR_DIR_RE = re.compile(r"^\d{4}$")

# Fields shared by every dataset of the collection. Override with --template
# and/or --field NAME=VALUE.
DEFAULT_FIELDS: dict = {
    "instrument_id": "DIS-01",
    "instrument_name": "Disdrómetro",
    "instrument_brand": "OTT",
    "instrument_model": "Parsivel²",
    "measurement_type": "Lectura automática",
    "variable_id": "DIS-01",
    "temporal_frequency": "mensual",
    "measured_variables": [
        "Intensidad de la lluvia / Rain intensity",
        "SYNOP4680",
        "Reflectividad del radar / Radar reflectivity",
        "Número de partículas / Particles number",
    ],
    "measurement_unit": "mm/h",
    "location": "HYO",
    "latitude": "-12.04",
    "longitude": "-75.32",
    "altitude": "3331",
    "data_format": "NetCDF",
    "instrument_status": "Operativo",
    "license_id": "cc-by",
    "url": "http://pnaig.igp.gob.pe/disdrometro",
    "version": "1.0",
    "author": "Yamina Silva",
    "author_email": "fsilva@igp.gob.pe",
    "maintainer": "Carlos del Castillo",
    "maintainer_email": "cdelcastillo@igp.gob.pe",
    "tag_string": "disdrometro,parsivel2,rain_intensity,precipitation,HYO",
    "private": False,
}

# Schema fields declared with `not_empty` that must be present before sending.
REQUIRED_FIELDS = (
    "title",
    "name",
    "dataset_start_timestamp",
    "dataset_end_timestamp",
    "temporal_frequency",
    "instrument_id",
    "instrument_name",
    "variable_id",
    "measurement_unit",
    "location",
    "data_format",
    "owner_org",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create one pnaig_clima_dataset dataset per YYYY directory and "
            "upload its monthly DIS_YYYYMM.nc files as resources."
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
        "--directory",
        type=Path,
        default=DEFAULT_DIRECTORY,
        help="Root directory holding one YYYY sub-directory per year.",
    )
    parser.add_argument(
        "--year",
        metavar="YYYY",
        type=int,
        action="append",
        default=[],
        help="Only process this year (repeatable). Default: every YYYY "
        "sub-directory found.",
    )
    parser.add_argument(
        "--owner-org",
        default=DEFAULT_OWNER_ORG,
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
        "--resource-description-template",
        default=DEFAULT_RESOURCE_DESCRIPTION_TEMPLATE,
        help="Template for each uploaded resource description. Extra "
        "placeholders: {month}, {month_es}, {file_name}, {file_stem}.",
    )
    parser.add_argument(
        "--include",
        metavar="GLOB",
        action="append",
        default=[],
        help="Only upload files matching this glob inside each year "
        f"directory (repeatable). Default: {DEFAULT_INCLUDE}",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="Create the datasets as private.",
    )
    parser.add_argument(
        "--on-existing",
        choices=("skip", "update", "duplicate"),
        default="skip",
        help="What to do when a dataset already has a resource with the "
        "same name: skip it, update (re-upload) it, or create a duplicate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the payloads and files that would be sent without "
        "creating or uploading anything.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Do not verify the TLS certificate of the CKAN instance.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Per-request timeout in seconds (uploads can be slow).",
    )

    args = parser.parse_args(argv)

    if not args.ckan_url:
        parser.error("--ckan-url is required (or set CKAN_URL).")
    if not args.api_token:
        parser.error("--api-token is required (or set CKAN_API_TOKEN).")
    if not args.directory.is_dir():
        parser.error(f"--directory is not a directory: {args.directory}")

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


def collect_years(directory: Path, only_years: list[int]) -> list[int]:
    """Return the sorted list of years present as YYYY sub-directories."""
    years = []
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() and YEAR_DIR_RE.match(entry.name):
            year = int(entry.name)
            if not only_years or year in only_years:
                years.append(year)
    if only_years:
        missing = sorted(set(only_years) - set(years))
        if missing:
            print(
                f"Warning: no directory for year(s) "
                f"{', '.join(map(str, missing))} in {directory}.",
                file=sys.stderr,
            )
    return years


def collect_files(year_dir: Path, includes: list[str]) -> list[Path]:
    """Return the sorted list of files to upload from a year directory."""
    import fnmatch

    patterns = includes or [DEFAULT_INCLUDE]
    return [
        path
        for path in sorted(year_dir.iterdir())
        if path.is_file()
        and not path.name.startswith(".")
        and any(fnmatch.fnmatch(path.name, glob) for glob in patterns)
    ]


def file_month(path: Path) -> tuple[int, int] | None:
    """Extract (year, month) from a ``DIS_YYYYMM.nc`` file name."""
    match = FILE_MONTH_RE.match(path.name)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return year, month


def temporal_coverage(
    year: int, files: list[Path]
) -> tuple[str, str]:
    """Temporal coverage as ISO timestamps derived from the file months.

    The start is the first day of the earliest ``DIS_YYYYMM`` file of the
    year; the end is the last day of the latest one. Falls back to the full
    calendar year when no file name can be parsed.
    """
    months = [
        month for file_year, month in
        (file_month(path) for path in files)
        if file_year == year
    ]
    if months:
        first_month, last_month = min(months), max(months)
    else:
        first_month, last_month = 1, 12
    last_day = calendar.monthrange(year, last_month)[1]
    start = f"{year:04d}-{first_month:02d}-01T00:00:00"
    end = f"{year:04d}-{last_month:02d}-{last_day:02d}T23:59:59"
    return start, end


def placeholders_for(shared: dict, year: int, start: str, end: str) -> dict:
    placeholders = {
        key: value
        for key, value in shared.items()
        if isinstance(value, (str, int, float))
    }
    placeholders.update(
        {"year": f"{year:04d}", "start_date": start[:10], "end_date": end[:10]}
    )
    return placeholders


def render(template: str, placeholders: dict, label: str) -> str:
    try:
        return template.format(**placeholders)
    except KeyError as exc:
        raise SystemExit(
            f"Unknown placeholder {exc} in {label}. Available: "
            + ", ".join(sorted(placeholders))
        )


def render_dataset_payload(
    year: int, files: list[Path], shared: dict, args: argparse.Namespace
) -> dict:
    """Build the package_create payload for one year."""
    start, end = temporal_coverage(year, files)
    placeholders = placeholders_for(shared, year, start, end)

    payload = dict(shared)
    payload.update(
        {
            "type": args.dataset_type,
            "name": render(args.name_template, placeholders, "--name-template").lower(),
            "title": render(args.title_template, placeholders, "--title-template"),
            "notes": render(args.notes_template, placeholders, "--notes-template"),
            "dataset_start_timestamp": start,
            "dataset_end_timestamp": end,
        }
    )
    return payload


def render_resource_description(
    path: Path, shared: dict, args: argparse.Namespace
) -> str:
    parsed = file_month(path)
    placeholders = {
        key: value
        for key, value in shared.items()
        if isinstance(value, (str, int, float))
    }
    placeholders.update(
        {
            "year": f"{parsed[0]:04d}" if parsed else "",
            "month": f"{parsed[1]:02d}" if parsed else "",
            "month_es": MONTHS_ES[parsed[1] - 1] if parsed else "",
            "file_name": path.name,
            "file_stem": path.stem,
        }
    )
    try:
        return args.resource_description_template.format(**placeholders)
    except KeyError:
        return args.resource_description_template


def validate_payload(payload: dict, year: int) -> None:
    missing = [
        field
        for field in REQUIRED_FIELDS
        if payload.get(field) in (None, "", [])
    ]
    if missing:
        raise SystemExit(
            f"Missing required field(s) for {year}: "
            + ", ".join(missing)
            + "\nProvide them with --owner-org, --template or --field NAME=VALUE."
        )


def guess_format(path: Path) -> str:
    """Best-effort CKAN ``format`` value derived from the file extension."""
    if path.suffix == ".nc":
        return "NETCDF"
    suffix = path.suffix.lstrip(".")
    if suffix:
        return suffix.upper()
    mimetype, _ = mimetypes.guess_type(path.name)
    return (mimetype or "").split("/")[-1].upper()


def dataset_exists(
    ckan: RemoteCKAN, name: str, requests_kwargs: dict
) -> dict | None:
    """Return the package dict when a dataset with this name/id exists.

    ``package_create`` cannot be trusted to reject duplicates: on some CKAN
    setups it silently updates the existing dataset, which would overwrite
    curated metadata. Always check first.
    """
    try:
        return ckan.call_action(
            "package_show", {"id": name}, requests_kwargs=requests_kwargs
        )
    except NotFound:
        return None


def existing_resources(package: dict) -> dict[str, dict]:
    """Map resource name -> resource dict for the given package."""
    return {
        resource.get("name") or "": resource
        for resource in package.get("resources", [])
    }


def upload_file(
    ckan: RemoteCKAN,
    package_id: str,
    path: Path,
    resource_name: str,
    description: str,
    requests_kwargs: dict,
    resource_id: str | None = None,
) -> dict:
    """Create or update a single resource with the contents of ``path``."""
    data_dict = {
        "name": resource_name,
        "format": guess_format(path),
        "description": description,
        "url": "",
    }
    if resource_id:
        action = "resource_update"
        data_dict["id"] = resource_id
    else:
        action = "resource_create"
        data_dict["package_id"] = package_id

    mimetype, _ = mimetypes.guess_type(path.name)
    with path.open("rb") as file_obj:
        return ckan.call_action(
            action,
            data_dict,
            files={
                "upload": (
                    path.name,
                    file_obj,
                    mimetype or "application/octet-stream",
                )
            },
            requests_kwargs=requests_kwargs,
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shared = load_shared_fields(args)

    years = collect_years(args.directory, args.year)
    if not years:
        print(f"No YYYY directories to process in {args.directory}.")
        return 0

    plan: list[tuple[int, dict, list[Path]]] = []
    for year in years:
        files = collect_files(args.directory / f"{year:04d}", args.include)
        payload = render_dataset_payload(year, files, shared, args)
        validate_payload(payload, year)
        plan.append((year, payload, files))

    requests_kwargs: dict = {"timeout": args.timeout}
    if args.insecure:
        requests_kwargs["verify"] = False

    ckan = RemoteCKAN(args.ckan_url, apikey=args.api_token, user_agent=USER_AGENT)

    print(
        f"{len(plan)} dataset(s) of type '{args.dataset_type}' "
        f"from {args.directory}."
    )

    datasets_created = datasets_existing = resources_created = 0
    resources_updated = resources_skipped = failed = 0

    for year, payload, files in plan:
        name = payload["name"]

        if args.dry_run:
            print(f"\n{year}: dataset {name} [dry-run]")
            print(
                "  payload  "
                + json.dumps(payload, ensure_ascii=False, sort_keys=True)
            )
            for path in files:
                print(
                    f"  upload   {path.name} "
                    f"({path.stat().st_size} bytes) [dry-run]"
                )
            continue

        try:
            package = dataset_exists(ckan, name, requests_kwargs)
        except (
            NotAuthorized,
            CKANAPIError,
            requests.exceptions.RequestException,
        ) as exc:
            print(f"  FAILED   {name}: {exc}", file=sys.stderr)
            failed += 1
            continue

        if package:
            print(f"\n{year}: exists {name} -> {package['id']}")
            datasets_existing += 1
        else:
            try:
                package = ckan.call_action(
                    "package_create", payload, requests_kwargs=requests_kwargs
                )
            except (
                ValidationError,
                NotAuthorized,
                NotFound,
                CKANAPIError,
                requests.exceptions.RequestException,
            ) as exc:
                print(f"  FAILED   {name}: {exc}", file=sys.stderr)
                failed += 1
                continue
            print(f"\n{year}: create {name} -> {package['id']}")
            datasets_created += 1

        if not files:
            print(f"  no files matching include glob in {year:04d}/")
            continue

        known = existing_resources(package)

        for path in files:
            resource_name = path.name
            description = render_resource_description(path, shared, args)
            current = known.get(resource_name)

            if current and args.on_existing == "skip":
                print(f"  skip     {resource_name} (already exists)")
                resources_skipped += 1
                continue

            resource_id = (
                current["id"]
                if current and args.on_existing == "update"
                else None
            )
            verb = "update" if resource_id else "upload"

            try:
                resource = upload_file(
                    ckan,
                    package["id"],
                    path,
                    resource_name,
                    description,
                    requests_kwargs,
                    resource_id=resource_id,
                )
            except (
                ValidationError,
                NotAuthorized,
                NotFound,
                CKANAPIError,
                requests.exceptions.RequestException,
                OSError,
            ) as exc:
                print(f"  FAILED   {resource_name}: {exc}", file=sys.stderr)
                failed += 1
                continue

            known[resource_name] = resource
            if resource_id:
                resources_updated += 1
            else:
                resources_created += 1
            print(f"  {verb:8} {resource_name} -> {resource['id']}")

    if args.dry_run:
        print(f"\nDry run: {len(plan)} dataset(s) rendered, nothing created.")
        return 0

    print(
        f"\nDone: {datasets_created} dataset(s) created, "
        f"{datasets_existing} already existing, {resources_created} "
        f"resource(s) uploaded, {resources_updated} updated, "
        f"{resources_skipped} skipped, {failed} failed."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
