# HTTP Caching Design

How responses are cached by browsers and CloudFront. Distinct from the server-side
disk slice cache documented in `cache_analysis.md` — that one bounds origin work;
this one bounds _network traffic to_ the origin.

---

## Goals

1. **Aggressive caching of expensive responses** (tile PNGs, legend images, the
   per-date data manifest) — these are deterministic functions of their URL, so
   they can safely live in caches for a long time.
2. **Fresh-when-needed for mutable catalogs** (products list, manifest of available
   dates) — the URL is fixed but the content changes when admins update products
   or when new dates arrive in Zarr. ETag-based revalidation keeps these correct
   without burning bandwidth on unchanged responses.
3. **Single source of truth at the origin.** The FastAPI app sets `Cache-Control`
   on every response; CloudFront and browsers honour it. No TTLs configured at
   the CDN layer that could drift from what the app declares.
4. **CDN absorbs concurrent load.** Multi-user reads of the same resource within
   a freshness window must hit CloudFront, not the origin.

---

## Two caches, one chain

```
┌─────────┐    ┌────────────┐    ┌────────┐
│ Browser │ ──►│ CloudFront │ ──►│FastAPI │
│ cache   │    │ edge cache │    │ origin │
└─────────┘    └────────────┘    └────────┘
```

`Cache-Control` set by the origin propagates through both layers. CloudFront's
Min/Max TTL are guardrails that clamp the origin's value; if the origin says
`max-age=300`, CloudFront uses 300 even if Max TTL is 1 year.

---

## Strategy per endpoint group

Endpoints fall into two categories based on whether their URL is
content-addressed or fixed-with-mutable-content.

### Category A — Content-addressed, long cache + `immutable`

The URL contains all inputs that determine the response bytes. Once rendered,
the bytes never change (subject to the operational rule in §"Invariant").

| Endpoint                                        | URL inputs                                          |
| ----------------------------------------------- | --------------------------------------------------- |
| `/data_tiles/{p}/{d}/{z}/{x}/{y}.png`           | product_id, date, tile coords                       |
| `/data_tiles/{p}/{d}/manifest.json`             | product_id, date                                    |
| `/visual_tiles/{p}/{d}/{z}/{x}/{y}.{ext}`       | + colormap, rescale, ext                            |
| `/visual_tiles/{p}/{d}/bbox.{ext}`              | + bbox, width, height, crs                          |
| `/visual_tiles/colormaps/{name}/legend`         | colormap name + rescale, width, height, orientation |
| `/{product_id}/{date}/point`                    | product_id, date, lat, lon                          |

**Header:** `Cache-Control: public, max-age=31536000, immutable` (1 year — the standard for content-addressed URLs)

`immutable` blocks browser revalidation even on user-triggered reload (Cmd+R).
Safe because the URL determines the bytes; staleness on any output-altering
change is handled by `CACHE_VERSION` (see §"Cache version invalidation").

### Category B — Mutable, ETag + short freshness window

The URL is fixed but the content changes (admin updates, new dates arriving).

| Endpoint                            | Why it mutates                               |
| ----------------------------------- | -------------------------------------------- |
| `/manifest` (products availability) | New dates arrive; admins add/remove products |

**Header:** `Cache-Control: public, max-age=300, must-revalidate` + `ETag`

Five-minute freshness window lets CloudFront absorb concurrent reads. After
the window, clients revalidate via `If-None-Match` — unchanged → `304`,
changed → `200` with new body.

ETag is a weak hash of a cheap fingerprint (product IDs + count + latest date
per product) built during response construction. See `_etag` and
`_etag_response` in `routers/products.py`.

`GET /products` is intentionally **not cached** — it's an ad-hoc admin / Swagger
endpoint, not on the frontend hot path, so caching would burn cycles for no
benefit.

#### How the ETag round-trip works

An ETag is a server-issued fingerprint of a response. Conditional GET uses two
headers — `ETag` (response) and `If-None-Match` (request) — to skip the body
when nothing has changed.

**First request — server issues an ETag:**

```
GET /manifest                          ▶
                                       ◀  200 OK
                                          ETag: W/"a3f9c1abcdef1234"
                                          Cache-Control: public, max-age=300, must-revalidate
                                          { "products": {...}, "cache_version": "cv1" }
```

The client stores the body and the ETag value alongside it.

**Revalidation (after the 5-min freshness window) — client asks "still
current?":**

```
GET /manifest                          ▶
If-None-Match: W/"a3f9c1abcdef1234"
                                       ◀  304 Not Modified
                                          ETag: W/"a3f9c1abcdef1234"
                                          (no body)
```

The browser (or CloudFront) **automatically** attaches its stored ETag as
`If-None-Match`. The server recomputes the current ETag, sees a match, and
returns `304` with no body — the client reuses its cached copy.

**When something changed** (new date arrived, admin updated a product, or
`CACHE_VERSION` was bumped) — the server's fingerprint differs:

```
GET /manifest                          ▶
If-None-Match: W/"a3f9c1abcdef1234"
                                       ◀  200 OK
                                          ETag: W/"b7e2f4567890abcd"
                                          { "products": {...new...}, "cache_version": "cv2" }
```

New body, new ETag. The client replaces its cached entry.

**Where this lives in code (`routers/products.py`):**

- `if_none_match: str | None = Header(None, alias="if-none-match")` — FastAPI
  reads the request header and binds it to the function parameter. `None` if
  the client didn't send it (first request).
- `_etag(fingerprint)` — computes the weak ETag (16-hex SHA1 prefix, wrapped
  as `W/"..."`). `W/` marks it as a _weak_ tag: semantic equivalence, not
  byte-identical, which is what we want — different orderings of equivalent
  fingerprints can still match.
- `_etag_response(body, etag, if_none_match)` — does the comparison: returns
  `304` with no body if the values match, `200` with the body if they differ.
  The `ETag` and `Cache-Control` headers are set in both branches.

The server is **stateless** about ETags. It doesn't remember what it returned
last time; it re-derives the current ETag every request and compares to
whatever the client sent. The client handles storage and re-sending.

---

## Current implementation map

| File                  | Symbol                                           | What                                                                            |
| --------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------- |
| `routers/shared.py`   | `IMMUTABLE_CACHE_HEADERS`                        | Category A — shared by all content-addressed endpoints across the three routers |
| `routers/products.py` | `_REVALIDATE_HEADERS`, `_etag_response`, `_etag` | Category B — `/manifest`, `/products`                                           |

All cache decisions live in the two constants above. To change the policy for
a category, edit one constant; do not configure TTLs at CloudFront.

---

## Invariant (the contract Category A relies on)

For `immutable` to be safe, one rule: **bump `CACHE_VERSION` on any change
that alters what the server would render for an existing URL.** Three triggers:

- **Renderer code** — colormap interpolation, PNG encoder, projection algorithm,
  data normalisation.
- **Product config update under the same ID** — `source_path` or `variable`
  changing.
- **Colormap definition update under the same name** — LUT contents changing.

In each case the URL is unchanged but the response bytes would differ.
`CACHE_VERSION` solves this by giving the frontend a new value to append to
URLs, producing a fresh URL space that's a cache miss everywhere.

---

## Cache version invalidation

**Status:** server side live; frontend integration still required. Until the
frontend appends `?cv=...` to URLs, bumping `CACHE_VERSION` is inert.

### Server side

- `CACHE_VERSION` lives in `constants.py`. Single source of truth.
- Published in the `/manifest` response body as `cache_version`.
- Included in the manifest's ETag fingerprint, so a bump forces clients to
  revalidate `/manifest` and pick up the new value within the 5-minute window.
- Tile/legend endpoints **do not read `?cv=...`** — FastAPI ignores undeclared
  query params and the server returns identical bytes regardless. The
  cache-busting effect comes from the URL change, not server-side logic:

  ```
  curl '.../3/4/2.png?colormap=viridis'                # same bytes
  curl '.../3/4/2.png?colormap=viridis&cv=cv1'         # same bytes
  curl '.../3/4/2.png?colormap=viridis&cv=anything'    # same bytes
  ```

  Browsers and CloudFront treat the three URLs as distinct cache entries
  because cache keys are URL-based. Old `cv` values must keep working so
  clients with a stale `/manifest` (within their 5-min window) aren't broken.

### Frontend contract

The frontend is the active half of this mechanism. Required:

1. **On app load**, fetch `/manifest`; store `cache_version`.
2. **On every tile, bbox, and legend URL**, append `?cv=$cache_version`
   alongside any existing query params (e.g. `?colormap=viridis&cv=cv1`).
3. **On every subsequent `/manifest` revalidation**, if `cache_version`
   changes: drop in-memory tile state and refetch visible tiles with the new
   value.

The frontend never validates or parses `cv` — it's a string passed through to
URLs verbatim.

### Flow

```
Day 1 — CACHE_VERSION = "cv1"
  Frontend URLs: .../3/4/2.png?colormap=viridis&cv=cv1
  Browser + CloudFront cache these (immutable, 1 year).

Day 30 — bump CACHE_VERSION to "cv2"; deploy
  Within 5 min, frontend revalidates /manifest, sees "cv2".
  New URLs: .../3/4/2.png?colormap=viridis&cv=cv2  → cache miss everywhere
  Old URLs: .../3/4/2.png?colormap=viridis&cv=cv1  → orphaned; LRU evicts
```

Worst-case staleness on an output-altering change: **5 minutes** (the manifest
freshness window). Without `cv`, up to a year.

### Why URL versioning, not CloudFront invalidation

|                          | CloudFront invalidation     | URL versioning              |
| ------------------------ | --------------------------- | --------------------------- |
| Evicts CDN cache         | yes                         | yes (cache miss on new URL) |
| Evicts browser cache     | **no** — up to a year stale | yes — instant on URL change |
| Requires frontend change | no                          | yes (append `?cv=...`)      |
| Cost                     | per-path API calls          | $0                          |

Browser cache invalidation is the load-bearing requirement, and only URL
versioning achieves it.

### Operational prerequisite (CloudFront)

CloudFront cache policy must forward query strings and include them in the
cache key — otherwise `cv`, `colormap`, and `rescale` collapse to one cache
entry. Already required today for `colormap`/`rescale`; verify once.

---

## Operational rules

1. **Bump `CACHE_VERSION` on any change that alters rendered output** (renderer
   code, product config under the same ID, or colormap definition under the
   same name). Document the change in the commit message.
2. **All cache policy lives in `Cache-Control` headers at the origin.** Don't
   configure TTLs at CloudFront beyond Min/Max guardrails — edit the constants
   in `routers/` instead.
