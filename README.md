# HAR to OpenAPI Converter

**English** | [Russian](README.ru.md)

## HAR Studio: local web interface

API projects, analysis of multiple HAR files, route review, immutable versions,
and comparison of observations between releases. Everything runs on your computer.
Use the **EN / RU** switch in the header to choose the interface language.
Messages and exported HTML reports use the selected language; captured data and
project names are left unchanged.

```bash
uv sync --locked
uv run --locked python -m studio.web
```

Open **http://127.0.0.1:8765**. The server listens only on the local address. To use
another port, pass `--port 8766`. Press `Ctrl+C` to stop the server.

### Your first project

1. Create a project, such as “Shop API”.
2. Select and upload one or more HAR files. A background task shows progress and
   the current filename, then opens domain selection automatically.
3. Select the relevant domains, excluding third-party services.
4. Review the results and suggestions to merge routes containing numeric IDs or UUIDs.
   Accept a suggestion, edit the parameter name, or keep the routes separate.
5. Expand an operation: each field shows observed types, occurrence counts, and a
   “Why this conclusion?” link to the source file, HAR entry number (starting at 1),
   and masked example.
6. Name and save the version. The ZIP export contains OpenAPI for each server,
   a standalone HTML report, and `evidence.json` with the full set of observations.

### Subsequent imports

- **Extend the current version** combines existing and new observations. The result
  is saved as a separate snapshot; the original version remains unchanged.
- **Compare a new version** analyzes only the new HAR files and compares them with
  the selected baseline. Old observations do not increase the new sample's counts.
- Saved route decisions apply to new IDs. You can revisit them in a draft. Saving
  without accepting suggestions is allowed: those routes remain separate.
- Any two saved versions of a project can be compared on the “Compare versions” page.

Comparison shows newly observed or unobserved operations, statuses, fields, and
changes in observed types. Absence from a sample **does not prove removal** or a
breaking contract change. Both samples are compared using the newer version's route
decisions. An import for comparison considers only the selected domains; comparing
saved versions includes all domains in those snapshots.

Comparison displays **Before / After** panels with highlighted field types and sample
counts. If the baseline has at least 10 requests and the new sample is less than
half its size, a notice calls out the smaller sample. This is a review aid, not a
confidence score. For saved version pairs, mark a change as **expected**, optionally
add a note, or return it to review. Decisions belong to the ordered version pair and
do not modify either snapshot.

### Workspace management

- Open **Manage project** to rename a project or archive it. Archived projects are
  listed under **Archive**; their versions remain readable and comparable. Unarchive
  a project before importing more captures.
- Delete unused drafts from the project or draft page. A confirmation checkbox is
  required; saved versions are not deleted.
- Open **Backups** to download a compressed `.json.gz` copy of all active and archived
  projects, versions, drafts, route decisions, and expected-change notes.
- Restore a backup through the same page. Validated data is added as **new projects**
  with new IDs; existing records are never overwritten. Relationships between
  restored versions are preserved. Repeated restoration creates additional copies.
  The upload limit is 50 MB, with a 200 MB uncompressed limit.
- Backups exclude original HAR files, worker jobs, and derived report caches. A
  running import is included only after it has produced a draft. Backups can still
  contain sensitive captured data; masking remains name-based.

### Try the examples

`examples/before.har` and `examples/after.har` contain only synthetic data.
Upload `before.har`, merge `/users/{user_id}` and `/orders/{order_id}`, and save
“Before release”. Then select “Compare a new version” and upload `after.har`.
The report will show `total` changing from a number to a string, a newly observed
409 response, and an unobserved `email` field. `/users/me` remains a separate route.

### Data and analysis boundaries

- Projects, drafts, decisions, and snapshots are stored in
  `.har2openapi/workspace.sqlite3` (excluded from Git). Use
  `--data-dir /path/to/workspace` to choose another directory. Use **Backups** for a
  portable copy while the app is running, or stop the server and copy the whole
  data directory. Existing workspaces are migrated automatically on startup.
- HAR uploads are not retained as separate original files. The database contains
  observations with masked examples, schemas, source filenames, and entry numbers.
  Name-based masking cannot remove every possible secret from text and URL paths.
- Counts use unique entries: uploading an identical file again does not double them.
  Different files containing overlapping requests may still include duplicates.
- Field occurrence is measured among bodies with the same direction, MIME type,
  and response status. For array fields, presence in at least one element counts
  once per entry: one response with email on just 1 of 100 users means 1 of 1
  responses, not 100 of 100 users. The overview groups fields by body, uses paths
  such as users[].email, and omits intermediate object/array nodes. Empty containers
  and mixed scalar/container types remain visible. Full evidence is retained.
  Frequency does not establish that a field is required.
- Object schemas are merged by property, and arrays by item schema. Incompatible
  types use `anyOf`. During Studio export, identical repeated object schemas are
  extracted into `components/schemas` and referenced with `$ref`. Names are stable
  content hashes, and observed examples are preserved. UUID recognition is used
  for route suggestions. The CLI export behavior is unchanged.
- Limits: 50 MB per import, 10,000 entries per file, and 20,000 unique entries per
  snapshot. Import and report analysis run in a local background worker. Up to four
  tasks can be queued or running at once; one task runs at a time. Progress during
  HAR conversion is based on processed entries; other stages show coarse progress.
  Analysis reports are cached in SQLite across page reloads and restarts (up to 100
  reports); changed observations or route rules produce a new cache key.
- The interface is intended for one user and one running server per workspace.
  Upload bytes are held only for the active job, not saved for automatic retry.
  After an interrupted process, unfinished jobs are marked failed and must be
  retried. Errors identify the failing file; failed batches do not create partial
  drafts. A completed export or restore still runs in the web request.
- Route decisions and versions survive restarts. After restarting the server,
  refresh open pages before submitting forms.

### Project structure

- `har2openapi.py`: shared conversion logic, independent of the interface.
- `studio/analysis.py`: observations, schema merging, and comparison.
- `studio/store.py`: migrations, SQLite persistence, snapshots, and reviews.
- `studio/jobs.py`: worker queue and persistent analysis cache.
- `studio/backup.py`: bounded backup reading and validation.
- `studio/export.py`: reusable OpenAPI components.
- `studio/i18n.py`, `studio/locales/`: interface translations.
- `studio/web.py`, `studio/templates/`, `studio/static/`: the web interface.

## Command-line converter

Convert HTTP Archive (HAR) captures into OpenAPI 3.0.3 YAML, with one file per
origin (scheme, host and port).

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
From the project directory:

```bash
uv sync --locked
```

`.python-version` selects Python 3.12. Dependencies live in `pyproject.toml`, and
`uv.lock` records their exact versions. Commit both files when changing dependencies.
The converter runs as scripts and needs no package build backend. To install only
runtime dependencies, use `uv sync --locked --no-dev`.

## Usage

```bash
uv run --locked python main.py capture.har -o output
uv run --locked python main.py capture.har --url-filter example.com --cookies sid --ignore-headers User-Agent,Cookie -o output
uv run --locked python main.py capture.har --url-filter '^https://api\.example\.com/.*' --sensitive-names credential,private_key
```

Run without a filename for interactive prompts:

```bash
uv run --locked python main.py
```

| Argument | Purpose |
| --- | --- |
| `filename` | HAR file; omit for interactive mode |
| `--url-filter` | Exact domain, optionally with port, or regex starting with `^` |
| `--cookies` | Comma-separated cookie names to keep; default: all |
| `--ignore-headers` | Comma-separated header names to omit, case-insensitive |
| `-o`, `--output-dir` | Output directory; created if needed; default: current directory |
| `--sensitive-names` | Additional field, query parameter or header names to mask |
| `--include-secrets` | Explicitly disable masking of examples |

Files have names such as `openapi_https_example.com.yaml` and
`openapi_https_example.com_8443.yaml`. Existing files with the same name are
replaced. No matches produce a message and no new files. Input/file errors return
exit code 1; argument errors return exit code 2.

## Conversion behavior

- Requests and responses use their own media types. JSON and `+json` content gets
  inferred object, array, string, boolean, integer, number and nullable schemas.
- Empty objects/arrays, `false`, `0`, empty strings and JSON `null` are preserved.
  Absent request bodies are omitted. Text content stays text.
- Query parameters preserve empty and repeated values. Cookies are deduplicated
  and represented as `in: cookie` parameters. Ignoring `Cookie` also excludes cookies
  from the HAR cookie list.
- Header matching is case-insensitive. `Authorization`, `Proxy-Authorization`,
  `Accept`, `Content-Type`, `Host`, `Content-Length` and HTTP/2 pseudo-headers are not
  emitted as header parameters. Authentication schemes are not yet inferred.
- Repeated operations combine response statuses and body media types. Different
  observed body schemas are collected with `anyOf`; the last example is retained
  for each media type. Parameter examples use the first observed value.
- Required fields cannot be determined from captures, so parameters, bodies and
  object properties are not marked required.
- Unknown/failed HTTP status codes use `default`, rather than a fabricated `200`.
  HEAD, 204 and 304 responses omit body content. Base64 responses use a byte schema
  without an encoded example.

## Secret masking

Masking is enabled by default for all cookie values and recognized sensitive names
in headers, query parameters, JSON and URL-encoded form bodies. Name matching ignores
case, hyphens and underscores. Recognized names include `authorization`, `password`,
`passwd`, `secret`, `session`, `sessionid`, `sid`, and names ending in `token`,
`apikey`, `secret` or `password`. Extend this set with `--sensitive-names`.

String secrets become `[REDACTED]`. Other sensitive values use type-compatible empty
values (`0`, `false`, `{}`, `[]`, or `null`). URL credentials are removed from server
URLs, and operation summaries contain no captured URLs or query strings.

This is name-based masking, not complete anonymization: secrets embedded in paths,
free text, unrecognized fields, or other header values can remain. Invalid JSON is
kept as raw text. Inspect generated files before sharing them. `--include-secrets`
disables example masking, but does not restore omitted headers or URL credentials.

## Python API

```python
from har2openapi import har2openapi

converter = har2openapi(
    filename="capture.har",
    url_filter="example.com",  # also accepts a list of domains
    cookie_filter=["sid"],
    ignore_headers=["User-Agent"],
    output_dir="output",
    mask_secrets=True,
    sensitive_names=["credential"],
)
files = converter.create_openapi()  # list[pathlib.Path]
```

## Development and validation

```bash
uv sync --locked
uv run --locked python -m pytest -q
uv run --locked openapi-spec-validator output/openapi_https_example.com.yaml
```

Tests validate generated documents with
[openapi-spec-validator](https://openapi-spec-validator.readthedocs.io/en/latest/python.html)
and body/parameter examples with `openapi-schema-validator`, alongside CLI and
regression checks. These packages and pytest are development dependencies;
the converter uses PyYAML, and the local web interface uses Flask.

```bash
uv add <package>
uv add --dev <development-package>
uv lock --upgrade
uv sync --locked
```

## Current limitations

For the command-line converter, captured examples describe observed traffic, not the complete API contract. Path
parameters, authentication schemes and required fields need manual review. Multipart
and HAR `postData.params` bodies are not yet converted; only captured body `text` is
used. Binary response bodies are not decoded. Non-HTTP URLs and unsupported methods
are rejected. Unusual HAR extensions may require normalization before conversion.
