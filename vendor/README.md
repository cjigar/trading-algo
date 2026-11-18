# Vendored third-party wheels

## `neo_api_client-2.0.0-py3-none-any.whl` — Kotak Neo Trade API SDK

**Why this is vendored.** `pyproject.toml`'s `[broker]` extra points at
`git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2`. On **2026-11-05** GitHub
blocked that repository:

```
GET https://api.github.com/repos/Kotak-Neo/Kotak-neo-api-v2  ->  403
{"message": "Repository access blocked",
 "block": {"reason": "private_information", "created_at": "2026-11-05T15:48:27Z"}}
```

The v1 repo (`Kotak-Neo/Kotak-neo-api`) returns 404, and the SDK has never been published to
PyPI (`neo_api_client`, `neo-api-client`, `kotak-neo-api` all 404). An unauthenticated clone
therefore fails the Docker build with:

```
fatal: could not read Username for 'https://github.com': No such device or address
```

**Provenance.** This wheel is **neo-api-client 2.0.0** — note the pyproject URL pins tag
`v2.0.2`, but 2.0.0 is the build that the last known-good `algo-trading:latest` image actually
shipped. It was repacked from that image's installed tree
(`/usr/local/lib/python3.11/site-packages/neo_api_client` + its `.dist-info`), with
install-time artifacts (`INSTALLER`, `direct_url.json`) and bytecode excluded and `RECORD`
regenerated. It is a pure-Python wheel (`Root-Is-Purelib: true`, `Tag: py3-none-any`), so it is
platform- and arch-independent.

Verified in a clean `python:3.11-slim`: installs cleanly, and `NeoAPI` exposes both
`totp_login` and `totp_validate` (the methods `broker/auth.py` drives).

**If upstream returns**, revert `Dockerfile` to `pip install ".[postgres,broker]"` and delete
this wheel. Until then, treat this file as the only source of the SDK — **do not delete it**.
