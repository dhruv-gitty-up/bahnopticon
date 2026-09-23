#!/usr/bin/env python3
"""Seed the BahnOpticon PostGIS tables from the local GeoJSON caches."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRACKS_PATH = PROJECT_ROOT / "data" / "tracks_cache_nationwide.geojson"
DEFAULT_STATIONS_PATH = PROJECT_ROOT / "data" / "stations_cache.geojson"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upsert BahnOpticon track and station caches into Supabase PostGIS."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL"),
        help="PostgreSQL URL. Defaults to SUPABASE_DB_URL, then DATABASE_URL.",
    )
    parser.add_argument("--tracks", type=Path, default=DEFAULT_TRACKS_PATH)
    parser.add_argument("--stations", type=Path, default=DEFAULT_STATIONS_PATH)
    parser.add_argument(
        "--dataset",
        choices=("all", "tracks", "stations"),
        default="all",
        help="Select which cache to seed (default: all).",
    )
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Delete existing rows in the selected table(s) before seeding.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and count cache rows without connecting to PostgreSQL.",
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if not args.dry_run and not args.database_url:
        parser.error("set SUPABASE_DB_URL or DATABASE_URL, or pass --database-url")
    return args


def load_features(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"GeoJSON cache does not exist: {path}")
    with path.open("r", encoding="utf-8") as source:
        payload = json.load(source)
    if payload.get("type") != "FeatureCollection" or not isinstance(
        payload.get("features"), list
    ):
        raise ValueError(f"Expected a GeoJSON FeatureCollection in {path}")
    return payload["features"]


def feature_id(feature: dict[str, Any], index: int, kind: str) -> str:
    properties = feature.get("properties") or {}
    candidates = (
        feature.get("id"),
        properties.get("station_id") if kind == "station" else None,
        properties.get("id"),
    )
    value = next((candidate for candidate in candidates if candidate not in (None, "")), None)
    if value is None:
        raise ValueError(f"{kind} feature {index} has no id")
    return str(value)


def validate_position(position: Any, label: str) -> None:
    if not isinstance(position, list) or len(position) < 2:
        raise ValueError(f"{label} is not a coordinate position")
    longitude, latitude = position[:2]
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (longitude, latitude)):
        raise ValueError(f"{label} contains a non-finite coordinate")
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise ValueError(f"{label} falls outside WGS84 longitude/latitude bounds")


def track_rows(features: Iterable[dict[str, Any]]) -> Iterator[tuple[str, str, str]]:
    for index, feature in enumerate(features):
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if geometry.get("type") != "LineString" or not isinstance(coordinates, list) or len(coordinates) < 2:
            raise ValueError(f"track feature {index} is not a valid LineString")
        for coordinate_index, position in enumerate(coordinates):
            validate_position(position, f"track feature {index} coordinate {coordinate_index}")
        product = (feature.get("properties") or {}).get("product")
        if not isinstance(product, str) or not product.strip():
            raise ValueError(f"track feature {index} has no product")
        yield feature_id(feature, index, "track"), product, json.dumps(geometry, separators=(",", ":"))


def station_rows(features: Iterable[dict[str, Any]]) -> Iterator[tuple[str, str | None, bool, str]]:
    for index, feature in enumerate(features):
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if geometry.get("type") != "Point":
            raise ValueError(f"station feature {index} is not a Point")
        validate_position(coordinates, f"station feature {index}")
        properties = feature.get("properties") or {}
        name = properties.get("name")
        if name is not None and not isinstance(name, str):
            name = str(name)
        yield (
            feature_id(feature, index, "station"),
            name,
            bool(properties.get("is_important", False)),
            json.dumps(geometry, separators=(",", ":")),
        )


def batched(rows: Iterable[tuple[Any, ...]], size: int) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def validate_dataset(path: Path, row_factory: Any) -> int:
    features = load_features(path)
    count = sum(1 for _ in row_factory(features))
    print(f"Validated {count:,} rows from {path}")
    return count


def seed_dataset(
    connection: Any,
    path: Path,
    row_factory: Any,
    statement: str,
    template: str,
    batch_size: int,
) -> int:
    from psycopg2.extras import execute_values

    features = load_features(path)
    total = 0
    with connection.cursor() as cursor:
        for batch in batched(row_factory(features), batch_size):
            execute_values(cursor, statement, batch, template=template, page_size=batch_size)
            connection.commit()
            total += len(batch)
            print(f"Seeded {total:,}/{len(features):,} rows from {path.name}", flush=True)
    return total


def main() -> int:
    args = parse_args()
    selections = {
        "tracks": args.dataset in ("all", "tracks"),
        "stations": args.dataset in ("all", "stations"),
    }

    if args.dry_run:
        if selections["tracks"]:
            validate_dataset(args.tracks, track_rows)
        if selections["stations"]:
            validate_dataset(args.stations, station_rows)
        return 0

    try:
        import psycopg2
    except ImportError as error:
        raise SystemExit(
            "psycopg2 is required: python -m pip install -r scripts/requirements.txt"
        ) from error

    connection = psycopg2.connect(args.database_url, application_name="bahnopticon-seed")
    try:
        if args.truncate:
            tables = [name for name, selected in selections.items() if selected]
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE " + ", ".join(f"public.{table}" for table in tables))
            connection.commit()

        if selections["tracks"]:
            seed_dataset(
                connection,
                args.tracks,
                track_rows,
                """
                    INSERT INTO public.tracks (id, product, geom) VALUES %s
                    ON CONFLICT (id) DO UPDATE SET
                        product = EXCLUDED.product,
                        geom = EXCLUDED.geom
                """,
                "(%s, %s, extensions.ST_SetSRID(extensions.ST_GeomFromGeoJSON(%s), 4326))",
                args.batch_size,
            )
        if selections["stations"]:
            seed_dataset(
                connection,
                args.stations,
                station_rows,
                """
                    INSERT INTO public.stations (id, name, is_important, geom) VALUES %s
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        is_important = EXCLUDED.is_important,
                        geom = EXCLUDED.geom
                """,
                "(%s, %s, %s, extensions.ST_SetSRID(extensions.ST_GeomFromGeoJSON(%s), 4326))",
                args.batch_size,
            )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"Seed failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
