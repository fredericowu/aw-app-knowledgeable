---
name: aw-knowledgeable
description: >-
  How to use the 13 MCP tools this app (Knowledgeable Connector) exposes
  for fredericowu/aw-knowledgeable — the standalone Neo4j knowledge-graph
  service, gateway-prefixed aw__aw_knowledgeable__*. Covers knowledge_search
  (the main reasoning/multi-query retrieval tool), the lower-level
  search_graph/search_nodes/get_graph/list_documents/upload_document/
  create_node/create_link graph tools, the fs-sync pending-deletion queue
  (list_pending_deletions/approve_pending_deletions/decline_pending_deletions),
  and the execution-history projection (search_execution_graph/
  exec_run_detail). Load this skill whenever a task needs to search or
  curate this workspace's knowledge graph, or needs any tool named above.
---

# aw-knowledgeable — the Knowledgeable Connector's own tool surface

This app (**Knowledgeable Connector**, id `knowledgeable`) is a thin HTTP
client in front of [fredericowu/aw-knowledgeable](https://github.com/fredericowu/aw-knowledgeable),
a standalone Neo4j knowledge-graph service. It has no Neo4j driver and no
Cypher of its own — every tool call here is a REST call to that service.
MCP Gateway aggregates this app's tools under the prefix
**`aw__aw_knowledgeable__<tool>`** (or `mcp__aw-gateway__aw__aw_knowledgeable__<tool>`
depending on how the gateway is mounted in your session).

## How the underlying service works, in one paragraph

Documents get uploaded (or synced from the filesystem/Notion/exec history)
into a Neo4j graph: each becomes a `Document` node, chunked and embedded.
Retrieval then has three independent strategies — **vector** (embedding
similarity, finds things worded differently but meaning the same), **lexical**
(BM25 full-text, finds exact terms), and **graph** (follows `LINKS_TO`/
`RELATED_TO` edges between documents). `knowledge_search` runs all three and
fuses them with Reciprocal Rank Fusion; the lower-level tools let you pick
one strategy yourself. Everything is scoped by **bucket** (a named partition
of the graph — omit it to search every bucket your token can read) and,
separately, by **collection** (a source-path folder prefix).

## Tool reference

### Primary retrieval — start here

| Tool | Use it for |
|---|---|
| `knowledge_search(query)` | **The default choice for any real question.** Decomposes `query` into sub-questions, retrieves across all three strategies, fuses the results, and optionally writes one cited, closed-book answer. Bounded — one planning call, one fan-out, one fuse, one optional synthesis (plus `rounds` extra refine cycles if asked); it never loops on its own. |

Key knobs worth knowing before calling it:
- `mode` — `fused` (default, all three arms), or pin to `vector`/`lexical`/`graph` alone.
- `limit` — **floor 20, not negotiable.** A lower value is silently clamped up because this corpus measurably returns a *worse result set* below 20, not a shorter prefix of a better one.
- `budget_ms` — default 20000, capped at 25000 (the production tunnel cuts requests around 30s regardless). Past budget, synthesis is skipped and retrieval is still returned — never a timeout, never a 500.
- `synthesize` — set `false` for the cheap path: just fused chunks + citations, no LLM write-up.
- **Always read the response's `effective` and `clamped` fields.** They say what *actually* ran — a knob you sent is advisory, not a guarantee. A real example: at low budget the planner call can be skipped (`clamped` says why) and the synthesis call can time out (`answer` comes back `null`) while the fused `chunks`/`citations` are still fully populated and usable.
- `include_qa` (default true) — the curated Q&A registry is searched alongside the document arms. Q&A hits come back in `chunks` with **`source: "qa"`** (plus a `qa_id`; the text is `Q: …` / `A: …`, there is no real document behind one). Pass `include_qa: false` to exclude them, or `mode: "qa"` to search only Q&A. Skipped automatically when `collection` or document `filters` are set.
- `include_code=true` additionally asks the workspace's code graph (when installed) and returns it in a separate `code` section — never fused into `chunks`, a disjoint key space from the knowledge graph.

### Q&A registry — curated, vetted answers

| Tool | Use it for |
|---|---|
| `qa_search(query?, tags?, tags_mode, limit, include_disabled)` | Look up the vetted answer to a known kind of question, or browse Q&A by tag. Give `query`, `tags`, or both (at least one). `tags_mode` is `any` (default) or `all`; `limit` defaults to 5 (cap 50). Returns `{items: [{id, question, answer, tags, origin, score, …}]}`; `origin` is `manual` (an operator wrote it in the Q&A page), `app` (an installed app contributed it) or `agent` (an agent proposed it and a person approved it). Disabled items are hidden unless `include_disabled` is true. Read-only. |
| `qa_propose_create(question, answer, tags?, reason?)`, `qa_propose_update(id, question?, answer?, add_tags?, remove_tags?, reason?)`, `qa_propose_delete(id, reason?)`, `qa_propose_tag_rename(from, to, reason?)` | **Propose** a change. Nothing is live or searchable until a person approves it in the Pending review section of the Q&A page; each returns `{proposal_id, status: "pending", duplicate}`. Run `qa_search` first to avoid duplicates. `qa_propose_update` is also the per-item tag add/remove tool. App-contributed items (`origin` `app`) are refused with 409: change them in the app's `contributes.qa` file. You cannot approve your own proposal. |
| `qa_list_proposals(status?, limit?)` | See what is pending and how decided ones ended (status, decision note, `result_item_id`). |

`knowledge_search` already folds Q&A in (see `include_qa` above); reach for `qa_search` when you want only the curated answers.

**For app authors — contributing Q&A.** In your `aw-app.json` declare `"contributes": {"qa": {"path": "qa/items.json"}}`. The file is a JSON list:

```json
[{"id": "stable-id", "question": "…", "answer": "…", "tags": ["my-app"]}]
```

`id` is yours and must stay stable (it keys the row); tags are lower-cased. The Knowledgeable Connector mirrors every installed app's file every few minutes — exact mirror: delete an item from the file and it disappears, edit it and it is re-indexed. A malformed item is rejected with a reason in the Connector log and never blocks the others; operators can disable an app item but not edit or delete it.

### Lower-level graph retrieval and curation

| Tool | Use it for |
|---|---|
| `search_graph(q, mode)` | Pick the retrieval strategy yourself instead of letting `knowledge_search` fuse them: `tree` (default — beam-descends the bucket's topic tree), `semantic` (pure vector), `lexical` (case-insensitive substring match on node *labels* — same mechanism as `search_nodes`, not BM25 full-text over chunk content). Has knobs `knowledge_search` doesn't: `beam_width` (tree only), `min_score` (semantic/tree), `anchor`/`anchor_depth` (start the traversal from a known node id), `related_vias`. |
| `search_nodes(q)` | Cheap, case-insensitive **substring match on node labels** — not content. Use for "does a node named roughly X exist", e.g. picking a link target before calling `create_link`. |
| `get_graph(focus)` | The neighbourhood (nodes + edges) around one **known node id**, 1 or 2 hops out. Use once you already have an id and want its context, not for open-ended search. |
| `list_documents()` | List every document node your token can read. Use to browse what's in the graph. |
| `upload_document(filename, content_base64)` | Add a new PDF/DOCX/TXT/MD (≤10MB) into the graph. |
| `create_node(label, type)` | Add a standalone entity node (person/agent/system/concept/...). |
| `create_link(from_id, to_id, type)` | Relate two existing nodes (documents or entities). Both ids must already exist in this connector's own tenant — a missing or cross-tenant id answers 404, never 403. |

### fs-sync pending-deletion queue — two-phase delete, handle with care

A filesystem sync tick only ever *marks* a document pending-delete; nothing is
actually removed from the graph until explicitly approved.

| Tool | Use it for |
|---|---|
| `list_pending_deletions()` | See what's queued for deletion, grouped by parent-directory prefix, before acting. Always call this first. |
| `approve_pending_deletions(bucket, expected_count, ...)` | **Permanently, irreversibly deletes** a scoped subset. Requires `bucket` and at least one of `path_prefix`/`node_ids` — there is deliberately no "approve everything." `expected_count` is mandatory: the call recomputes the match count itself and refuses (deleting nothing) if it doesn't equal what you passed. Always `list_pending_deletions` → count what you mean to approve → pass that count. |
| `decline_pending_deletions(bucket, ...)` | The reversible half — restores documents out of the pending-delete queue. Safe to call broadly: omitting both `path_prefix` and `node_ids` declines *everything* pending in that bucket, which is fine because decline only restores, never deletes. |

Every call to either tool is written to this connector's own audit log, including a rejected one.

### Execution-history projection — this workspace's own agent-run history

A separate, structurally disjoint subgraph (no shared labels, no edges into
the knowledge corpus) projecting agents-platform run metadata — only
populated if the connector's exec-history sync is enabled.

| Tool | Use it for |
|---|---|
| `search_execution_graph(q, target)` | Full-text search over past agent runs. `target='runs'` searches a run's own input/output prose; `'tool_calls'` searches tool-call names+arguments (use the bare, hyphen-free tool name, e.g. `approve_pending_deletions` not the full gateway-prefixed id — the full id is one indexed token a bare search would miss); `'events'` searches the free-text event kinds (`error`, `log`, `thinking`, `node_end`) and must be requested explicitly, it is not in the `'both'` default. |
| `exec_run_detail(run_id)` | One run in full: metadata, scrubbed input/output, every tool call, its chronological event timeline, and its structural joins (parent/child/QA runs). 404 if the run isn't in this tenant. |

## Quick usage guidance for any LLM

1. **Default to `knowledge_search`.** It is the one tool built to take a plain
   question and do the right thing — don't hand-roll a `search_graph` call
   unless you specifically need one retrieval strategy or a knob
   `knowledge_search` doesn't expose (anchor, beam_width, min_score).
2. **Always read `effective`/`clamped` on a `knowledge_search` response** before
   assuming the parameters you sent are the parameters that ran.
3. **Use `search_nodes` only for existence checks by name**, not for answering
   a real question — it matches labels, not content.
4. **Treat `approve_pending_deletions` as irreversible.** Call
   `list_pending_deletions` first, and always pass the exact `expected_count`
   you computed from that list.
