# Target index schema (`target_index.json`)

Version: 1.

`PandoraTargetList` owns all target metadata: coordinates, magnitudes, camera settings, and transit ephemerides live in its `target_definition_files/` tree and are read from there at use time. This package keeps a lightweight index, `data/target_index.json`, that links every target name (and alias) seen in calendars and reports to its definition file(s), so lookups do not require searching the tree.

## Example

```json
{
  "schema_version": 1,
  "generated_utc": "2026-08-21T00:00:00Z",
  "targets": {
    "gj_367": {
      "aliases": ["GJ_367", "GJ_367b", "GJ 367"],
      "definition_files": [
        {"category": "auxiliary-exoplanet", "path": "auxiliary-exoplanet/GJ_367b_target_definition.json", "file_version": "1.0.0"}
      ]
    },
    "g4476152832143994112": {
      "aliases": ["G4476152832143994112"],
      "definition_files": [
        {"category": "auxiliary-standard", "path": "auxiliary-standard/G4476152832143994112_target_definition.json", "file_version": "1.0.0"}
      ]
    }
  },
  "unresolved": ["SOME_CALENDAR_TARGET"]
}
```

## Field rules

| Field | Notes |
|---|---|
| `targets.<target_key>` | key is the normalized name produced by `targets.py` from any alias (lowercase, separators collapsed) |
| `aliases` | every raw spelling this target has appeared under in calendars, sequences, and reports, plus the names inside its definition files (`Star Name`, `Planet Name`) |
| `definition_files[]` | one entry per category the target appears in (targets are intentionally in multiple categories). `path` is relative to the `PandoraTargetList` `target_definition_files/` directory. `file_version` is the definition file's own `Version` field at index time, so a stale index is detectable. |
| `unresolved` | target names seen in ingested files that could not be matched to any definition file. Kept visible rather than dropped; these need a human to add the target or the alias. |

## Behavior

- The `PandoraTargetList` checkout location comes from config (`target_list_dir`); paths in the index are relative so the index works across machines.
- The index is built by scanning `target_definition_files/` and is rebuilt on demand (`pandora-obs rebuild-target-index`). A lookup miss during ingest triggers a rescan before landing in `unresolved`.
- Consumers (transit counting in `rollups.py`, notebooks) resolve a target through the index, then read the definition file directly for whatever they need (ephemerides, camera settings). Transit epochs in those files are BJD_TDB and must be converted before comparison with UTC record timestamps.
- The Python package `pandoratargetlist` is not imported; its import chain pulls heavy simulation dependencies. The JSON definition files are the interface.
