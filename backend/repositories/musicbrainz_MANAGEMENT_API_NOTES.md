# MusicBrainz Library Management API notes

Verified against the live production JSON API on 2026-07-21. The response `Date`
header was `Tue, 21 Jul 2026`; requests used DroppedNeedle's descriptive user agent.

## Canonical release lookup

The representative request was:

```text
GET /ws/2/release/aff0622e-7bd3-4fb6-9ca3-0fa19dd2340b
    ?fmt=json
    &inc=artist-credits+recordings+release-groups+labels+isrcs+aliases
         +artist-rels+work-rels+recording-rels+recording-level-rels
         +release-group-level-rels+work-level-rels+genres
```

It returned HTTP 200 and the selected Goldberg Variations release. The decoded
surface required by Library Management was present:

- release: `id`, `title`, `status`, `date`, `country`, `barcode`, `asin`,
  `packaging`, `artist-credit`, `label-info`, `media`, `release-group`, `genres`,
  and `relations`;
- release group: `id`, `title`, `first-release-date`, `primary-type`,
  `secondary-types`, credits, aliases, genres, and relations;
- artist credit items: credited `name`, `joinphrase`, and an artist with `id`,
  canonical `name`, `sort-name`, aliases, and genres;
- label info: `catalog-number` plus a nullable label object;
- medium: `id`, numeric `position`, optional `title`, `format`, `track-count`, and
  `tracks`;
- release track: its own `id`, numeric `position`, display `number`, `title`,
  optional `length`, artist credit, and `recording`;
- recording: its distinct `id`, title, length, ISRCs, credits, aliases, genres, and
  relations. Recording relationships included artist/instrument credits and work
  performance links; linked works carried their own relationships.

The response contained 34 tracks and was about 608 KB with all relationship and
genre includes. Production requests must therefore construct the smallest sorted
include set required by the selected profile; the full verification include set is
not a default.

The release-track `track.id` is not the recording MBID. Management must retain both
and must never derive one from the other.

## Release-track titles and recording titles

Re-verified against the live production API on 2026-07-29 with Anthony Green's
_Avalon_ release `0687c8a5-40a2-4a0c-bdc9-c1d80d94bef5` and the `recordings` and
`artist-credits` includes. Track 14 (`741e8bcd-9d03-3b61-bb04-ecf22f4784e1`) returned
the release-track title `The Fisherman Will Be Bewildered`, while its linked recording
(`ec935e35-b2fa-4925-aa83-052d9e3e69f1`) returned the distinct title
`The Fishermen Will Be Bewildered`.

Edition-specific surfaces and Library Management must prefer the release-track title.
The recording title is a fallback only when the release track omits its own title.

## Nullable packaging identifiers

Re-verified against the live production API on 2026-07-28 with release
`6f0026c8-d310-4905-990d-47fae8a06542` and the minimal identity-readiness include set
(`artist-credits`, `recordings`, and `release-groups`). The response returned HTTP 200
with both `packaging` and `packaging-id` explicitly set to JSON null. Multiple other
selected releases returned the same shape during a live readiness run. Both fields must
therefore remain nullable in the provider model.

## Nullable catalogue numbers

Re-verified against the live production API on 2026-07-28 with Anthony Green's
_Avalon_ release `0687c8a5-40a2-4a0c-bdc9-c1d80d94bef5` and the complete include set
required by the Picard-style Organizer profile. The response returned HTTP 200 and a
41,819-byte JSON document. Its first `label-info` entry contained a label object while
`catalog-number` was explicitly JSON null. The catalogue-number field must therefore
remain nullable independently of the label object.

## Nullable work type identifiers

Re-verified against the live production API on 2026-08-03 with release
`8c66c2ac-bcb3-4b5a-8a0a-f0d5f24ceed2` and the relationship includes used by the
Picard-style Organizer. The performance relationship for work
`fa1f9350-3d27-35c8-abc2-a4455cf68d24` returned both `work.type` and
`work.type-id` explicitly set to JSON null. Linked work type identifiers must
therefore be nullable and must not make an otherwise valid canonical release
fail decoding.

## Nullable status and release-group primary-type identifiers

Re-verified against the live production API on 2026-08-15 with the minimal
reconciliation include set (`artist-credits`, `recordings`, and
`release-groups`). Release `04cd3c24-5622-4470-a77a-a338b7998b34` ("I Fought the
Law") returned both `status` and `status-id` explicitly set to JSON null, and
release `8f1dd604-321c-4376-a383-818f4e5ab3f5` ("Haunt Me", release-group
`9e984086-d190-4a3d-a137-adaf1617327a`) returned both `primary-type` and
`primary-type-id` explicitly set to JSON null on its release group. Each of
these pairs previously split: the display string was nullable while the
identifier was modelled as required, and the deterministic decode failure
poisoned the shared circuit breaker. Identifier fields for both must therefore
remain nullable. A relationship-rich probe of the same release on the same date
returned null only for relation `begin`/`end` (already nullable), so relation
`type-id` fields stay required until a live payload proves otherwise.

## Missing and malformed identifiers

- A syntactically valid but nonexistent release UUID returned HTTP 404 with JSON
  `{"error":"Not Found", ...}`.
- The all-zero UUID returned HTTP 400 with JSON `{"error":"Invalid mbid.", ...}`.

The repository model must distinguish definitive absence from malformed input and
from transport/provider failure. Tests use sanitized fixtures and never call the live
service.

The existing project-wide MusicBrainz one-request-per-second limiter, priority queue,
retry/circuit-breaker wrapper, and request deduplicator remain authoritative.

## Artist-credit projection

Re-verified against the live production JSON API on 2026-07-31 using release
`aff0622e-7bd3-4fb6-9ca3-0fa19dd2340b` with the minimal reconciliation includes
`artist-credits+recordings+release-groups`. The release artist credit returned two
ordered entries, each with a stable artist `id`, canonical `name`, `sort-name`, credited
`name`, and an exact `joinphrase` (`"; "` followed by `""`).

The first release track, `fecc7c25-4896-3498-b6a3-da8f8aaaf93f`, returned its own
artist credit for Johann Sebastian Bach, while its linked recording,
`d57d7065-020f-4648-b1ca-12c9ba72f78d`, returned a distinct artist credit for Glenn
Gould. Reconciliation must therefore prefer a present release-track `artist-credit`,
fall back to the recording credit only when the track credit is absent, and retain the
release-track MBID separately from the recording MBID. Credit order, credited names,
and join phrases are provider evidence and must not be reconstructed from display text.

## Merged recording identifiers

Re-verified against the live production JSON API on 2026-08-10. MusicBrainz returned
HTTP 301 for retired recording MBIDs, and the configured HTTP client followed those
redirects to a normal recording document whose `id` was the canonical replacement.
Examples observed in the live library included:

- `5224cfc7-b3bb-4008-a41b-21b168dc631f` to
  `beaf82cd-24f9-4ce9-b1a9-022339a30f77`;
- `5bc2a5d4-c573-4a91-9a90-dc5709245628` to
  `04b0d80c-d9a6-4163-b89b-dd354858e89f`;
- `fc554903-eb86-46b9-a912-1b4a519656f6` to
  `593d7c47-e11e-4993-a0c6-0767dcdcaafd`.

Each replacement exactly matched the recording on the already accepted release.
Identity readiness may therefore treat a retired MBID as equivalent only after this
lookup proves the redirect target. A successful response for a different recording,
a missing response, or a provider failure is not proof and must not relax the normal
conflict gate. The retired value remains in the sealed evidence so Apply can validate
the exact alias that was reviewed.

## Exact release ownership

Re-verified against the live production JSON API on 2026-08-10 with Clairo's
_Immunity_ release `c85ad49c-6bfb-4bdc-96f8-f5f305a8799e`. A release request with
`recordings+artist-credits` returned the canonical release and all 11 tracks but omitted
the `release-group` member. Adding `release-groups` returned canonical release group
`fc97b087-221c-4ea4-9dd9-5277a52eb84a` in the same document.

Exact-release identification requires the provider-returned release group to prevent a
requested alias or edition from being attached to an assumed group. Its request must
therefore include `release-groups`; absence after that request remains a fail-closed
provider-evidence result.
