create schema if not exists extensions;
create extension if not exists postgis with schema extensions;

create table if not exists public.tracks (
    id text primary key,
    product text not null,
    geom extensions.geometry(LineString, 4326) not null
);

create table if not exists public.stations (
    id text primary key,
    name text,
    is_important boolean not null default false,
    geom extensions.geometry(Point, 4326) not null
);

create index if not exists tracks_geom_gix
    on public.tracks using gist (geom);

create index if not exists stations_geom_gix
    on public.stations using gist (geom);

-- These public-schema tables are written through a direct PostgreSQL connection.
-- Add narrowly scoped read policies before exposing them through the Data API.
alter table public.tracks enable row level security;
alter table public.stations enable row level security;
