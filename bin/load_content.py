#!/usr/bin/env python3
"""Bulk-upload every file in a local directory as resources of a CKAN dataset.

Standalone helper script (not part of the docker deployment): it talks to a
CKAN instance over HTTP through the ckanapi library, using the FileStore API
(``resource_create`` / ``resource_update`` with a multipart ``upload`` field).

See https://docs.ckan.org/en/2.12/maintaining/filestore.html#filestore-api

Example:

    .venv/bin/python bin/load_content.py \\
        --ckan-url http://localhost:5000 \\
        --package-id my-dataset \\
        --directory ./data \\
        --api-token "$CKAN_API_TOKEN"
"""

from __future__ import annotations

import argparse
import fnmatch
import mimetypes
import os
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

USER_AGENT = "pnaig-load-content/1.0 (+https://www.gob.pe/igp)"
DEFAULT_EXCLUDES = (".*", "*~", "*.tmp", "*.part", "Thumbs.db", "desktop.ini")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bulk-upload the files of a local directory as resources of an "
            "existing CKAN dataset."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--ckan-url",
        default=os.environ.get("CKAN_URL"),
        help="Base URL of the CKAN instance (env: CKAN_URL).",
    )
    parser.add_argument(
        "--package-id",
        required=True,
        help="id or name of the dataset that will receive the resources.",
    )
    parser.add_argument(
        "--directory",
        required=True,
        type=Path,
        help="Local directory holding the files to upload.",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("CKAN_API_TOKEN"),
        help="CKAN API token (env: CKAN_API_TOKEN).",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Walk sub-directories instead of only the top level.",
    )
    parser.add_argument(
        "--include",
        metavar="GLOB",
        action="append",
        default=[],
        help="Only upload files matching this glob (repeatable).",
    )
    parser.add_argument(
        "--exclude",
        metavar="GLOB",
        action="append",
        default=[],
        help="Skip files matching this glob (repeatable). Added to the "
        f"defaults: {' '.join(DEFAULT_EXCLUDES)}",
    )
    parser.add_argument(
        "--on-existing",
        choices=("skip", "update", "duplicate"),
        default="skip",
        help="What to do when the dataset already has a resource with the "
        "same name: skip it, update (re-upload) it, or create a duplicate.",
    )
    parser.add_argument(
        "--description",
        default="",
        help="Description applied to every uploaded resource.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be uploaded without contacting the FileStore.",
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


def collect_files(
    directory: Path,
    recursive: bool,
    includes: list[str],
    excludes: list[str],
) -> list[Path]:
    """Return the sorted list of files to upload from ``directory``."""
    pattern = "**/*" if recursive else "*"
    all_excludes = list(DEFAULT_EXCLUDES) + list(excludes)

    selected: list[Path] = []
    for path in sorted(directory.glob(pattern)):
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        if any(part.startswith(".") for part in relative.parts[:-1]):
            continue
        if any(fnmatch.fnmatch(path.name, glob) for glob in all_excludes):
            continue
        if includes and not any(
            fnmatch.fnmatch(path.name, glob) for glob in includes
        ):
            continue
        selected.append(path)
    return selected


def guess_format(path: Path) -> str:
    """Best-effort CKAN ``format`` value derived from the file extension."""
    suffix = path.suffix.lstrip(".")
    if suffix:
        return suffix.upper()
    mimetype, _ = mimetypes.guess_type(path.name)
    return (mimetype or "").split("/")[-1].upper()


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

    files = collect_files(
        args.directory, args.recursive, args.include, args.exclude
    )
    if not files:
        print(f"No files to upload in {args.directory}.")
        return 0

    requests_kwargs: dict = {"timeout": args.timeout}
    if args.insecure:
        requests_kwargs["verify"] = False

    ckan = RemoteCKAN(
        args.ckan_url, apikey=args.api_token, user_agent=USER_AGENT
    )

    try:
        package = ckan.call_action(
            "package_show",
            {"id": args.package_id},
            requests_kwargs=requests_kwargs,
        )
    except NotFound:
        print(f"Dataset not found: {args.package_id}", file=sys.stderr)
        return 1
    except NotAuthorized:
        print(
            f"Not authorized to read dataset {args.package_id}. "
            "Check the API token.",
            file=sys.stderr,
        )
        return 1
    except (CKANAPIError, requests.exceptions.RequestException) as exc:
        print(f"Could not reach CKAN at {args.ckan_url}: {exc}", file=sys.stderr)
        return 1

    known = existing_resources(package)
    print(
        f"Dataset '{package.get('name', args.package_id)}' "
        f"({len(package.get('resources', []))} existing resources) "
        f"- {len(files)} local file(s) found."
    )

    created = updated = skipped = failed = 0

    for path in files:
        resource_name = str(path.relative_to(args.directory))
        current = known.get(resource_name)

        if current and args.on_existing == "skip":
            print(f"  skip     {resource_name} (already exists)")
            skipped += 1
            continue

        resource_id = current["id"] if current and args.on_existing == "update" else None
        verb = "update" if resource_id else "upload"

        if args.dry_run:
            print(f"  {verb:8} {resource_name} ({path.stat().st_size} bytes) [dry-run]")
            continue

        try:
            resource = upload_file(
                ckan,
                package["id"],
                path,
                resource_name,
                args.description,
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
            updated += 1
        else:
            created += 1
        print(f"  {verb:8} {resource_name} -> {resource['id']}")

    if args.dry_run:
        print(f"\nDry run: {len(files)} file(s) inspected, nothing uploaded.")
        return 0

    print(
        f"\nDone: {created} created, {updated} updated, "
        f"{skipped} skipped, {failed} failed."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
