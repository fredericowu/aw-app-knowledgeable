"""Pushes the Q&A items installed apps contribute up to aw-knowledgeable's Q&A
registry (design ref ``qa-section-design-v2``) — the ``sources_sync`` twin.

An app declares ``contributes.qa = {"path": "qa/items.json"}`` in its
``aw-app.json``; the file is a JSON list of ``{id, question, answer, tags}``.
Every tick this module reads every installed app's file and ``PUT``s the whole
set to ``/api/qa/sync`` as an **exact mirror** for this workspace: a row whose
item disappeared from its file (or whose app was uninstalled) is removed by
the backend, and an unchanged row costs nothing there (no re-embed).

Isolation rules:

* One app's missing/malformed file or non-object item never blocks the
  others — it is logged and skipped.
* An item with a usable ``id`` but bad content is still forwarded, so the
  backend rejects it with a per-item reason (logged here) and — because the
  backend never deletes the previously-synced row of a *rejected* item —
  a typo in one item does not wipe its last good version.
* The file path is resolved inside the app's own directory; a path that
  escapes it is refused.

The service secret is re-read on every call through ``mcp.client`` (never
captured), and a 404 (backend without the Q&A routes) is a quiet, retried
"not deployed yet" state, same as the sources push.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable

import httpx

from . import forwarding
from .mcp import client
from .workspace_env import workspace_env

log = logging.getLogger("aw_apps.knowledgeable.qa_sync")

#: One unconditional full push per tick. The backend leaves unchanged rows
#: alone, so a short interval is cheap; a new/changed seed shows up within
#: this window of an app install or update.
INTERVAL_S = int(os.environ.get("KNOWLEDGEABLE_QA_SYNC_INTERVAL_S") or 360)

_config_resolver: Callable[[], dict] = lambda: {}
_package_dir: str | None = None


def configure(config_resolver: Callable[[], dict], package_dir: str | None) -> None:
    """Installed once from ``plugin.activate`` (same shape as
    ``sources_sync.configure``); ``package_dir`` only locates this app's own
    ``aw-app.json`` for ``connector_version``."""
    global _config_resolver, _package_dir
    _config_resolver = config_resolver
    _package_dir = package_dir


def _connector_version() -> str | None:
    if not _package_dir:
        return None
    try:
        with open(os.path.join(_package_dir, "aw-app.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        return manifest.get("version") if isinstance(manifest, dict) else None
    except (OSError, ValueError):
        return None


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class ScanFailed(Exception):
    """The apps directory could not be listed. Not the same as "no app
    contributes anything": an empty snapshot is an instruction to the backend
    to delete every app-origin row, so a failed scan must send nothing."""


def collect() -> tuple[list[dict], list[str]]:
    """Every installed app's declared Q&A items, wire-shaped
    (``app_slug``/``external_id``/``question``/``answer``/``tags``), plus the
    problems met on the way. A broken *app* contributes nothing and shows up
    in the problem list; an unlistable apps root raises ``ScanFailed``.

    Only canonical installed package directories are scanned: the directory
    name must be a valid app slug. The updater parks the previous package as
    ``<slug>.old`` for the length of a swap (and operators leave
    ``<slug>.stale-*-bak`` copies) — reading those would report the same app
    twice, with entries the new package removed. The app slug is the
    directory name for the same reason."""
    root = forwarding._apps_root()
    items: list[dict] = []
    problems: list[str] = []
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        raise ScanFailed(f"cannot list apps root {root}: {exc}") from exc

    for name in names:
        if not forwarding.APP_SLUG_RE.match(name):
            continue
        app_dir = os.path.join(root, name)
        if not os.path.isdir(app_dir):
            continue
        manifest_path = os.path.join(app_dir, "aw-app.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            manifest = _read_json(manifest_path)
        except (OSError, ValueError) as exc:
            problems.append(f"{name}: unreadable aw-app.json: {exc}")
            continue
        contributes = manifest.get("contributes") if isinstance(manifest, dict) else None
        spec = contributes.get("qa") if isinstance(contributes, dict) else None
        if spec is None:
            continue
        rel = spec.get("path") if isinstance(spec, dict) else None
        if not isinstance(rel, str) or not rel.strip():
            problems.append(f"{name}: contributes.qa must be an object with a non-empty 'path'")
            continue
        base = os.path.realpath(app_dir)
        full = os.path.realpath(os.path.join(base, rel))
        if full != base and not full.startswith(base + os.sep):
            problems.append(f"{name}: contributes.qa path '{rel}' escapes the app directory")
            continue
        try:
            doc = _read_json(full)
        except (OSError, ValueError) as exc:
            problems.append(f"{name}: cannot read Q&A file '{rel}': {exc}")
            continue
        if not isinstance(doc, list):
            problems.append(f"{name}: Q&A file '{rel}' must be a JSON list")
            continue
        for i, raw in enumerate(doc):
            ext = raw.get("id") if isinstance(raw, dict) else None
            if not isinstance(ext, str) or not ext.strip():
                problems.append(f"{name}: item #{i} skipped (needs an object with a non-empty string 'id')")
                continue
            items.append(
                {
                    "app_slug": name,
                    "external_id": ext,
                    "question": raw.get("question"),
                    "answer": raw.get("answer"),
                    "tags": raw.get("tags"),
                }
            )
    return items, problems


async def _put_sync(body: dict) -> tuple[dict | None, int, str | None]:
    url = f"{client.base_url()}/api/qa/sync"
    headers = {**client._headers(), "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60) as http_client:
            resp = await http_client.put(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return None, 0, f"could not reach aw-knowledgeable at {url}: {exc}"
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400:
        detail = (data or {}).get("detail") if isinstance(data, dict) else None
        return data, resp.status_code, (str(detail) if detail else f"HTTP {resp.status_code}")
    return data, resp.status_code, None


async def sync_once() -> dict:
    try:
        items, problems = collect()
    except ScanFailed as exc:
        log.warning("qa_sync: %s — skipping this push (a failed scan is not an empty snapshot)", exc)
        return {"status": "scan_failed", "error": str(exc), "item_count": 0, "problems": []}
    for problem in problems:
        log.warning("qa_sync: %s", problem)
    body = {
        "workspace_slug": workspace_env("AW_WORKSPACE") or "aw",
        "connector_version": _connector_version(),
        "items": items,
    }
    data, status, err = await _put_sync(body)
    if status == 404:
        log.info("qa_sync: PUT /api/qa/sync is 404 (backend without Q&A) — will retry next tick")
        return {"status": "not_deployed", "item_count": len(items), "problems": problems}
    if err:
        log.warning("qa_sync: push failed: %s", err)
        return {"status": "error", "error": err, "item_count": len(items), "problems": problems}
    rejected = (data or {}).get("rejected") if isinstance(data, dict) else None
    for r in rejected or []:
        log.warning(
            "qa_sync: backend rejected %s/%s: %s",
            r.get("app_slug"), r.get("external_id"), r.get("reason"),
        )
    return {"status": "ok", "item_count": len(items), "problems": problems, "server": data}


async def run_forever() -> None:
    """Background task from ``plugin.activate()``; the first tick runs
    immediately (the boot sync)."""
    while True:
        try:
            result = await sync_once()
            log.info("qa_sync: %s", {k: v for k, v in result.items() if k != "problems"})
        except Exception:  # noqa: BLE001 — a tick must never kill the loop
            log.exception("qa_sync: tick failed")
        await asyncio.sleep(INTERVAL_S)
