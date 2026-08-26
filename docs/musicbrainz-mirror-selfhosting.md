# Self-hosting a MusicBrainz mirror for DroppedNeedle

DroppedNeedle talks to MusicBrainz over the standard `ws/2` web service. By default it
points at `https://musicbrainz.org/ws/2` and honors MetaBrainz's published 1 request/second
limit. If you run your **own full MusicBrainz mirror** (the official
[metabrainz/musicbrainz-docker](https://github.com/metabrainz/musicbrainz-docker) stack),
you can point DroppedNeedle's existing *Settings → MusicBrainz* API URL at it instead. The
client already supports this end-to-end: lookups, browse calls, merge-redirect resolution,
and search all keep working unchanged against a caught-up mirror.

This guide covers what a mirror actually demands of you - hardware, cron jobs, licensing,
and exposure risk - without selling it as free wins. Everything here was verified against
upstream sources in August 2026; where a claim comes from an enthusiast guide rather than
MetaBrainz, that is stated.

---

## 1. What you are signing up for

A mirror is a full copy of the MusicBrainz PostgreSQL database plus the bundled
musicbrainz-server application that serves `ws/2` over HTTP. You trade dependence on one
shared public service for responsibility for your own replica:

| Responsibility | Reality |
|---|---|
| Disk | See sizing section below - plan for hundreds of GB |
| Replication | You apply upstream packets on a cron; data is only as fresh as your schedule |
| Search freshness | Search indexes are **not** part of replication; you reindex them yourself |
| Schema migrations | Upstream schema changes require you to migrate on your schedule |
| Security | The stack is not hardened for public exposure by default |

You do **not** need any Lidarr metadata layer for DroppedNeedle. Some community guides
bundle a Lidarr Metadata API service alongside musicbrainz-docker; DroppedNeedle speaks
plain `ws/2`, which the stock stack serves out of the box.

---

## 2. Pin the release

Use the same release the widely used deployment guides pin:

```
metabrainz/musicbrainz-docker  v-2026-05-13.0-mbdb31-pg18
```

This tag exists upstream (published 2026-05-13T20:34Z) and corresponds to MusicBrainz
Server schema sequence **31** on PostgreSQL 18.

Follow the upstream repo's own deployment docs (`admin/configure`, `createdb.sh`,
`replication.sh`) rather than copying commands from third-party pages - the upstream README
is the authority on flags and volume layout.

### Upgrading past the pin

At the time of writing, musicbrainz-docker HEAD has moved to `v-2026-07-30.1`. Falling
behind schema changes means migrating manually when you do upgrade. Before jumping pins,
read the upstream release notes for schema-sequence bumps and apply any documented
migration steps between your current pin and the target. Community forks sometimes publish
pin-to-pin migration notes (for example, the BrainzMash fork documents the migration into
the May pin); treat those as starting points and verify against upstream.

---

## 3. Replication token and cadence

Replication packets are how your mirror catches up with musicbrainz.org:

1. Sign up for the free non-commercial Live Data Feed token at
   <https://metabrainz.org/signup/noncommercial> and put it in your DBDefs/compose config.
2. Load the initial database with `createdb.sh -fetch` (expect an hour or more).
3. Run an initial `replication.sh` catch-up, then put `replication.sh` on a cron.

Cadence guidance:

- Upstream publishes packets **hourly** - that is the finest documented granularity; there
  is no realtime tier.
- musicbrainz-docker's default crontab applies them **daily at 03:00 UTC**.
- Running hourly (e.g. at :05 past) keeps typical lag around or under an hour, which is the
  official bound for a well-fed mirror ("can never be more than about an hour off sync").

---

## 4. Search indexing

Search results come from Solr indexes that replication does **not** carry. Two supported
options:

- **Weekly reindex cron** (the standard path):

  ```
  sir reindex --entity-type artist --entity-type release
  ```

  Put it on a weekly schedule. A full manual reindex measured ~4½ hours on a
  16-thread/16 GB machine.

- **Pre-built Solr index import**: MetaBrainz publishes prebuilt index archives as roughly a
  **60 GB download** - faster to start than building from scratch, and you still reindex
  weekly afterwards.

Live (continuous) indexing exists upstream but is explicitly marked experimental/"not
stable yet"; prefer the cron until that changes.

If your search indexes go stale, DroppedNeedle still works, but text matching degrades -
see [What a mirror changes](#what-a-mirror-changes-for-droppedneedles-queries) below.

---

## 5. Sizing honesty

Two different numbers circulate. Quote both to yourself before buying hardware:

| Source | CPU | RAM | Disk |
|---|---|---|---|
| Enthusiast guide prerequisites (hearring-aid) | "moderately capable" | ≥ 8 GB | ≥ 100 GB |
| musicbrainz-docker recommendation, indexed search | 16 threads | 16 GB | 350 GB |
| musicbrainz-docker floor, **no** indexed search | 2 threads | 4 GB | 100 GB |

The enthusiast minimums get there by capping container memory hard (a representative compose
override caps services at roughly 20 GB total across musicbrainz/db/indexer/cache) and
indexing only artists and releases. Indexed search on a small Solr heap is the first thing
to fall over under memory pressure.

Real datapoint: one operator loaded the full dump (224 tables, ~357 million rows) in about
35 minutes on an 8-core/16 GB VM, then the primary-key build step died on a 256 GB disk
with 171 GB free - an OOM-class failure during `CreatePrimaryKeys`. Leave real headroom on
disk beyond the raw dump size, especially if you co-host anything else (including
DroppedNeedle itself) on the same box.

---

## 6. Exposure warnings

- Bind the stack to localhost or your LAN. Do not port-forward it. The upstream README
  explicitly warns the services are **not configured to be safe for public exposure**.
- The bundled standalone Solr ships with **CVE-2025-24814**. Keep it off untrusted networks
  and track upstream for fixed images.
- musicbrainz-docker exposes **no built-in rate-limit knobs** (`DBDefs.pm` has none). The
  live site's throttling is edge infrastructure policy that a mirror does not reproduce.
  That means: whatever is fronting your mirror can be hammered freely unless *you* limit
  it - and once DroppedNeedle points at it, DroppedNeedle's own client-side limiter is the
  only guard in play. Configure it deliberately (next section).
- Reads over `ws/2` require no authentication on a mirror, so anyone who can reach the port
  can query it.

---

## 7. Licensing

MusicBrainz data is split across two licenses, and replication packets carry their own:

- **Core data - CC0** (public domain; the main `mbdump.tar.bz2`). Use it however you like.
- **Supplementary data - CC BY-NC-SA 3.0**: annotations, tags/genres, ratings, edit history,
  stats, and the derived dump.
- **Live Data Feed replication packets - CC BY-NC-SA 3.0.**

For personal/non-commercial use this is straightforward: get the free token and go.
Commercial use is handled through
[MetaBrainz supporter accounts](https://metabrainz.org/) - framed by MetaBrainz as a moral
rather than legal obligation, but take it seriously if your use is commercial.

---

## 8. Point DroppedNeedle at your mirror

In DroppedNeedle: **Settings → MusicBrainz**, set:

- **API URL**: `http://<mirror-host>:5000/ws/2` (the compose stack publishes port 5000;
  adjust if you remapped it). Any host speaking MusicBrainz `ws/2` works - the setting
  accepts any valid HTTP(S) URL.
- **Rate limit** and **Concurrent searches**: off-official endpoints accept wider values
  than musicbrainz.org, because you are now the one being polite to:

  | Field | Allowed range off-official |
  |---|---|
  | Rate limit | 0.1 – 500 requests/second, **or `0` = Unlimited** |
  | Concurrent searches | 1 – 64 |

  On your **own hardware**, raised limits or `0` (Unlimited) are legitimate - it is your
  machine and your disk. DroppedNeedle's priority lanes, request deduplication, circuit
  breaker, and caches stay fully active either way; only the client-side pacing bucket is
  affected by the sentinel.

  > Be reasonable with servers you don't own. `Unlimited` is meant for hardware you run.
  > Pointing `rate_limit = 0` at somebody else's box is the digital equivalent of letting
  > yourself into their house because the door was unlocked.

- Press **Test Connection**. It fires the same probe at whatever URL is configured.

Two things the official endpoint enforces that your mirror will not: edge per-IP throttling
and authentication (there is none for reads on a mirror either). If you expose the mirror
beyond localhost, put your own reverse-proxy auth and limits in front - see
[Exposure warnings](#exposure-warnings).

### Checking your data vintage

To see how far behind musicbrainz.org your mirror is, ask its database directly:

```sh
docker exec <mb-container> psql -U musicbrainz -d musicbrainz \
  -c "SELECT max(last_replication_date) FROM replication_control;"
```

Compare the timestamp against the current date. Hours-old is healthy for an hourly
replication cron; days-old means your replication cron is broken or stopped.

---

## 9. Community endpoints

The owner decision for DroppedNeedle sanctions community/external MusicBrainz servers as a
**user-owned choice, disclosed rather than forbidden**. Nothing in the app blocks or
downgrades identity decisions made while pointed at one - your informed selection *is* the
consent, and every accepted identity records which endpoint class produced it
(`provider_base_url` is stamped automatically; see the audit note below).

What follows is the honest risk picture, not a prohibition.

### Known candidates (assessed 2026-08-24)

| Candidate | Verdict | Grounds |
|---|---|---|
| BrainzMash pool (`dash.brainzmash.cc`) | Permitted, eyes-open | Majority-fingerprint quorum proves *agreement among volunteers*, not correctness: sybil members define truth, and every member runs the same patched image so correlated bad data passes quorum. One operator controls the single front door (trusted root - cache poisoning or selective responses bypass all member checks). The public dashboard displays other people's query strings (artist/album searches observed publicly, 2026-08-24). Lag enforcement claims conflict internally: README says 24 h threshold checked 6-hourly; dashboard said "Max allowed age: 4h". Quorum/eviction implementation is not public. |
| `api.musicinfo.pro` | Avoid | Probed 2026-08-24: 404 on every endpoint (Cloudflare-fronted, edge-cached). Operator identity unpublished anywhere. Open issue #84 independently reports stale data. No quorum, no dashboard, pure trust-in-a-stranger. |

A self-hosted mirror fed by the official PGP-signed dumps remains the strongest provenance
chain there is: provenance terminates at musicbrainz.org regardless of which machine served
the bytes.

### Protocol caveat

The currently known BrainzMash shared front door speaks the **Lidarr metadata dialect**
(`/api/v0.4/...`), *not* MusicBrainz `ws/2`. It is **not consumable** through DroppedNeedle's
MusicBrainz setting today, and a failed Test Connection against it is the correct outcome -
not a bug. If a public community mirror ever emerges that genuinely speaks `ws/2`, it plugs
straight into this setting with zero code changes.

### Audit trail

Every identity decision DroppedNeedle accepts from an automatic pass stamps the serving
endpoint (`provider_base_url`) alongside the existing decision source, so an audit query can
always answer "which class of server told us this". This record never gates anything; it
exists so you can review your own history later.

---

## What a mirror changes for DroppedNeedle's queries

DroppedNeedle makes three classes of MusicBrainz calls. They degrade differently on a
mirror, and knowing which is which tells you what to watch:

| Class | Count in DroppedNeedle | Mirror behavior |
|---|---|---|
| **Lookup** (MBID + `inc=` detail fetches) | 13 production sites | Byte-compatible on a caught-up mirror. Merged-entity MBID redirects are core musicbrainz-server behavior, served identically once replication has absorbed the merge. |
| **Browse** (paged entity listings) | 3 sites | Byte-compatible, same caveat: your replica must have caught up with the relevant edits. |
| **Search** (text queries ranked by Solr) | 7 sites | **Order drifts** with your reindex cadence. Search indexes are not replicated; ranking depends on your `sir reindex` schedule. Scores feed downstream candidate comparisons, so neglect here is visible as worse matching long before anything errors. |

Practical consequence: the duplicate-candidate recall paths - edition search when linking an
album, and the duplicate-search check during contributions - compare candidates downstream
and therefore **degrade first** if reindexing is neglected. If identification quality seems
to slip after moving to a mirror, your search index is the first suspect; run the reindex
cron manually and re-check before filing bugs.

---

*Sources for the dated claims above: metabrainz/musicbrainz-docker (README, releases API,
DBDefs.pm master snapshot), blampe/hearring-aid (guide + issues #84/#99/#103/#106/#75),
statichum/brainzmash-hearring-aid (code deltas, NGINX/bootstrap docs, dashboard snapshot),
musicbrainz.org documentation pages (MusicBrainz API, Database Download, Live Data Feed,
Data License, Canonical data), and data.metabrainz.org dump listings - all fetched
2026-08-24.*
