"""MOC sequence validation and ingest.

Ported from the MOCSeqGen ``compare_calendar_sequence.py`` script, with structured output.
A generated observation block starts at a science ``PANDORA GOTO_TARGET`` (``VEL_ABER 0,
PRI_REF_DIR 8, SEC_REF_DIR 2``) and ends at the terminating ``PANDORA PAYLOAD_READ`` with
``CCSDS_AP_ID HSDR, PL_APID 0, PATH '', PL_PATH ''``, which fires ``end_buffer_s`` before
the true end. Blocks are matched to requested observations by greatest time overlap with
target name as tiebreak; one request may be satisfied by several blocks.

Validation and ingest are separate operations (see ``docs/schemas/sequence-record.md``):
`validate_sequence` writes nothing under ``data/`` and is meant for draft sequences and MOC
pushback, while `ingest_sequence` records the final sequence and fills each matched
observation's ``scheduled`` block and status. Both write ``<seq_name>_validation_report.txt``
beside the sequence file and print the summary to the console.

Optional inputs: a ``ksat_contacts.json`` overlays ground-contact windows so a truncation
can be traced to the contact that caused it, and a telecom sequence (``T<yy>W<ww>.seq.json``)
is cross-checked against science (no time overlap allowed) and against the contacts.
"""

# Standard library
import json
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# First-party/Local
from pandoraobservations import logger
from pandoraobservations.calendars import DELIVERY_RE, parse_calendar
from pandoraobservations.database import ObservationDatabase, sha256_of_file
from pandoraobservations.schema import CalendarRecord

SEQUENCE_RECORD_VERSION = 1
DEFAULT_END_BUFFER_S = 45.0
# Telecom commands further apart than this start a new telecom activity block.
TELECOM_BLOCK_GAP_S = 600.0
# A telecom command this close to a science command is a collision in the merged timeline.
# (A telecom command merely falling inside a science observation window is NOT a conflict:
# that is the normal contact-interrupts-science case, visible as gap truncation.)
CONFLICT_TOLERANCE_S = 1.0
# Sequence deliveries are named S26W35.seq.json (science) / T26W35.seq.json (telecom).
SEQ_FILE_RE = re.compile(r"(?P<id>[STst]\d{2}[Ww]\d{2})\.seq\.json$")
# Mismatches on these sequence-side keys are noise and are never reported.
IGNORED_SEQ_KEYS = {"PLD_NUM_PREDEFINED_STAR_ROIS"}

# Calendar payload name -> sequence PLD_* parameter name, verbatim from the MOCSeqGen script.
INF_PARAM_MAP = {
    "AverageGroups": "PLD_AVERAGE_GROUPS",
    "ROI_StartX": "PLD_ROI_START_X",
    "ROI_StartY": "PLD_ROI_START_Y",
    "ROI_SizeX": "PLD_ROI_SIZE_X",
    "ROI_SizeY": "PLD_ROI_SIZE_Y",
    "RiceX": "PLD_RICE_X",
    "RiceY": "PLD_RICE_Y",
    "SaveImagesToDisk": "PLD_SAVE_IMAGES_TO_DISK",
    "SendThumbnails": "PLD_SEND_THUMBNAILS",
    "ThumbnailBinSize": "PLD_THUMBNAIL_BIN_SIZE",
    "ThumbnailCompressionType": "PLD_THUMBNAIL_COMPRESSION_TYPE",
    "TargetID": "PLD_TARGET_ID",
    "SC_Resets1": "PLD_SC_RESETS1",
    "SC_Resets2": "PLD_SC_RESETS2",
    "SC_DropFrames1": "PLD_SC_DROP_FRAMES1",
    "SC_DropFrames2": "PLD_SC_DROP_FRAMES2",
    "SC_DropFrames3": "PLD_SC_DROP_FRAMES3",
    "SC_ReadFrames": "PLD_SC_READ_FRAMES",
    "SC_Groups": "PLD_SC_GROUPS",
    "SC_Integrations": "PLD_SC_INTEGRATIONS",
}

VIS_PARAM_MAP = {
    "IncludeFieldSolnsInResp": "PLD_INCLUDE_FIELD_SOLNS_IN_RESP",
    "ROI_StartX": "PLD_ROI_START_X",
    "ROI_StartY": "PLD_ROI_START_Y",
    "ROI_SizeX": "PLD_ROI_SIZE_X",
    "ROI_SizeY": "PLD_ROI_SIZE_Y",
    "MaxMagnitudeInQuadCatalog": "PLD_MAX_MAGNITUDE_IN_QUAD_CATALOG",
    "SaveImagesToDisk": "PLD_SAVE_IMAGES_TO_DISK",
    "RiceX": "PLD_RICE_X",
    "RiceY": "PLD_RICE_Y",
    "SendThumbnails": "PLD_SEND_THUMBNAILS",
    "TargetID": "PLD_TARGET_ID",
    "TargetRA": "PLD_TARGET_RA",
    "TargetDEC": "PLD_TARGET_DEC",
    "StarRoiDetMethod": "PLD_STAR_ROI_DET_METHOD",
    "FramesPerCoadd": "PLD_FRAMES_PER_COADD",
    "ExposureTime_us": "PLD_EXPOSURE_TIME_US",
    "MaxNumStarRois": "PLD_MAX_NUM_STAR_ROIS",
    "StarRoiDimension": "PLD_STAR_ROI_DIMENSION",
    "NumTotalFramesRequested": "PLD_NUM_TOTAL_FRAMES_REQUESTED",
    "numPredefinedStarRois": "PLD_NUM_PREDEFINED_STAR_ROIS",
}


def to_utc(text: str) -> datetime:
    """Parse a calendar or sequence timestamp into an aware UTC datetime."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def fmt(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_top_level(text: str) -> list[str]:
    """Split a command parameter string on commas, respecting quotes and brackets."""
    parts, buf = [], []
    depth = 0
    in_quote = False
    for ch in text:
        if ch == "'":
            in_quote = not in_quote
            buf.append(ch)
        elif not in_quote and ch == "[":
            depth += 1
            buf.append(ch)
        elif not in_quote and ch == "]":
            depth -= 1
            buf.append(ch)
        elif not in_quote and depth == 0 and ch == ",":
            if "".join(buf).strip():
                parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def cast_value(raw: str):
    """Cast a sequence parameter to its natural type (quoted string, list, hex, int, float)."""
    text = raw.strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [cast_value(x) for x in split_top_level(inner)] if inner else []
    if re.fullmatch(r"0x[0-9A-Fa-f]+", text):
        return int(text, 16)
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if re.fullmatch(r"[-+]?\d*\.\d+(?:[eE][-+]?\d+)?", text) or re.fullmatch(r"[-+]?\d+[eE][-+]?\d+", text):
        return float(text)
    return text


def parse_command(raw: str) -> tuple[str, dict]:
    """Split a raw ``PANDORA <NAME> with K V, ...`` command into its name and parameters."""
    match = re.match(r"^PANDORA\s+([A-Z0-9_]+)(?:\s+(?:with|WITH)\s+(.*))?$", raw.strip())
    if not match:
        return raw.strip(), {}
    params = {}
    for part in split_top_level(match.group(2) or ""):
        if " " in part:
            key, value = part.split(" ", 1)
            params[key.strip()] = cast_value(value.strip())
    return match.group(1), params


@dataclass
class SequenceCommand:
    time: datetime
    raw: str
    name: str
    params: dict


@dataclass
class Block:
    """One generated observation block: science GOTO_TARGET through closing PAYLOAD_READ."""

    start: datetime
    stop: datetime
    target: str = ""
    science_file: str = ""
    terminated: bool = False
    inf_params: dict = field(default_factory=dict)
    vis_params: dict = field(default_factory=dict)

    def effective_stop(self, end_buffer_s: float) -> datetime:
        # The closing PAYLOAD_READ fires early by the buffer; an unterminated block has no
        # closing read, so its last command time is the best available stop.
        return self.stop + timedelta(seconds=end_buffer_s) if self.terminated else self.stop


def load_commands(path) -> tuple[list[SequenceCommand], bool]:
    """Load a ``.seq.json`` file into time-sorted commands, plus its ``useCosmos`` flag."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    commands = []
    for row in data.get("Commands", []) + data.get("Events", []):
        stamp, raw = row.get("TimeStamp"), row.get("Command", "") or row.get("Event", "")
        if stamp and raw:
            name, params = parse_command(raw)
            commands.append(SequenceCommand(time=to_utc(stamp), raw=raw, name=name, params=params))
    commands.sort(key=lambda c: c.time)
    return commands, bool(data.get("useCosmos", False))


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def is_science_goto(cmd: SequenceCommand) -> bool:
    return (
        cmd.name == "GOTO_TARGET"
        and _as_int(cmd.params.get("VEL_ABER")) == 0
        and _as_int(cmd.params.get("PRI_REF_DIR")) == 8
        and _as_int(cmd.params.get("SEC_REF_DIR")) == 2
    )


def is_end_payload_read(cmd: SequenceCommand) -> bool:
    """The observation-closing read: CCSDS_AP_ID HSDR, PL_APID 0, PATH '', PL_PATH ''."""
    return (
        cmd.name == "PAYLOAD_READ"
        and "PATH" in cmd.params
        and "PL_PATH" in cmd.params
        and str(cmd.params.get("CCSDS_AP_ID", "")).upper() == "HSDR"
        and _as_int(cmd.params.get("PL_APID")) == 0
        and str(cmd.params["PATH"]) == ""
        and str(cmd.params["PL_PATH"]) == ""
    )


def detect_blocks(commands: list[SequenceCommand]) -> list[Block]:
    """Group a command stream into generated observation blocks."""
    blocks: list[Block] = []
    current = None
    last_time = None

    def close(block: Block, stop: datetime, terminated: bool):
        block.stop = stop
        block.terminated = terminated
        block.target = block.target or "UNKNOWN"
        blocks.append(block)

    for cmd in commands:
        if is_science_goto(cmd):
            if current is not None:
                # The previous block never saw its closing PAYLOAD_READ; end it where it stood.
                close(current, last_time, terminated=False)
            current = Block(start=cmd.time, stop=cmd.time)
            last_time = cmd.time
            continue
        if current is None:
            continue
        last_time = cmd.time

        if cmd.name == "PAYLOAD_ACQUIRE_INF_CAM_IMAGES":
            current.inf_params = dict(cmd.params)
            target = cmd.params.get("PLD_TARGET_ID")
            if isinstance(target, str) and target:
                current.target = target
        elif cmd.name == "PAYLOAD_ACQUIRE_VIS_CAM_SCIENCE_DATA":
            current.vis_params = dict(cmd.params)
            target = cmd.params.get("PLD_TARGET_ID")
            if isinstance(target, str) and target and not current.target:
                current.target = target
        elif cmd.name == "PAYLOAD_READ":
            path = str(cmd.params.get("PATH", ""))
            if path and not current.science_file:
                # The opening read names the file science data lands in on the payload.
                current.science_file = path
            if is_end_payload_read(cmd):
                close(current, cmd.time, terminated=True)
                current = None

    if current is not None:
        close(current, last_time, terminated=False)
    return blocks


def overlap_s(a_start, a_stop, b_start, b_stop) -> float:
    return max(0.0, (min(a_stop, b_stop) - max(a_start, b_start)).total_seconds())


def _value_equal(a, b, tol=1e-6) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tol
    return str(a) == str(b)


def load_contacts(path) -> list[dict]:
    """Load a ``ksat_contacts.json`` into ``{ground_station, antenna, start, stop}`` dicts."""
    contacts = []
    for row in json.loads(Path(path).read_text(encoding="utf-8")):
        key = row.get("ksatContactsPK", {})
        contacts.append(
            {
                "ground_station": key.get("groundStation", ""),
                "antenna": row.get("antennaNumReserved") or row.get("antennaNumPreferred", ""),
                "start": to_utc(key["startTime"]),
                "stop": to_utc(row["endTime"]),
            }
        )
    contacts.sort(key=lambda c: c["start"])
    return contacts


def match_blocks(active, windows, blocks, end_buffer_s) -> tuple[dict, list]:
    """Assign every block to the request it overlaps most, target name as tiebreak.

    One request may be satisfied by several blocks (the sequence pauses and re-acquires),
    so this is one-to-many. Returns (request index -> block indices in time order,
    unassigned block indices).
    """
    assigned: dict[int, list[int]] = {}
    unassigned = []
    for j, block in enumerate(blocks):
        block_stop = block.effective_stop(end_buffer_s)
        best_i, best_key = -1, (0.0, 0)
        for i, (start, stop) in enumerate(windows):
            ov = overlap_s(start, stop, block.start, block_stop)
            if ov <= 0:
                continue
            same_target = 1 if active[i].target and active[i].target == block.target else 0
            if (ov, same_target) > best_key:
                best_key, best_i = (ov, same_target), i
        if best_i < 0:
            unassigned.append(j)
        else:
            assigned.setdefault(best_i, []).append(j)
    for indices in assigned.values():
        indices.sort(key=lambda j: blocks[j].start)
    return assigned, unassigned


def _coverage_and_truncation(start, stop, matched, end_buffer_s, start_tolerance_s):
    """Merge matched blocks clipped to the requested window; split the uncovered seconds."""
    requested_s = (stop - start).total_seconds()
    clipped = sorted(
        (max(b.start, start), min(b.effective_stop(end_buffer_s), stop))
        for b in matched
        if min(b.effective_stop(end_buffer_s), stop) > max(b.start, start)
    )
    coverage = []
    for c_start, c_stop in clipped:
        if coverage and c_start <= coverage[-1][1]:
            coverage[-1] = (coverage[-1][0], max(coverage[-1][1], c_stop))
        else:
            coverage.append((c_start, c_stop))

    covered = sum((s2 - s1).total_seconds() for s1, s2 in coverage)
    if coverage:
        start_late = (coverage[0][0] - start).total_seconds()
        end_early = (stop - coverage[-1][1]).total_seconds()
        gap = requested_s - covered - start_late - end_early
        if start_late <= start_tolerance_s:
            start_late = 0.0
    else:
        start_late, gap, end_early = 0.0, requested_s, 0.0
    return round(covered, 3), {
        "start_late_s": round(max(0.0, start_late), 3),
        "mid_gap_s": round(max(0.0, gap), 3),
        "end_early_s": round(max(0.0, end_early), 3),
    }


def _block_findings(obs, matched) -> tuple[list, list]:
    """Payload parameter mismatches and anomaly notes for one observation's blocks."""
    inf_requested = {k.split(".", 1)[1]: v for k, v in obs.payload.items() if k.startswith("AcquireInfCamImages.")}
    vis_requested = {k.split(".", 1)[1]: v for k, v in obs.payload.items() if k.startswith("AcquireVisCamScienceData.")}
    mismatches, notes = [], []
    for block in matched:
        if not block.terminated:
            notes.append(f"block starting {fmt(block.start)} has no closing PAYLOAD_READ")
        if obs.target and block.target and obs.target != block.target:
            notes.append(f"generated target {block.target} != requested {obs.target}")
        for prefix, requested, actual, mapping in (
            ("AcquireInfCamImages", inf_requested, block.inf_params, INF_PARAM_MAP),
            ("AcquireVisCamScienceData", vis_requested, block.vis_params, VIS_PARAM_MAP),
        ):
            for cal_key, seq_key in mapping.items():
                if seq_key in IGNORED_SEQ_KEYS or cal_key not in requested:
                    continue
                got = actual.get(seq_key)
                if seq_key not in actual or not _value_equal(requested[cal_key], got):
                    mismatches.append(
                        {"parameter": f"{prefix}.{cal_key}", "requested": requested[cal_key], "scheduled": got}
                    )
    return mismatches, notes


def _telecom_checks(commands, telecom_path, contacts) -> dict:
    """Cross-check the telecom sequence: timeline collisions and contact correlation."""
    telecom_commands, _ = load_commands(telecom_path)
    science_times = [cmd.time for cmd in commands]
    conflicts = []
    for cmd in telecom_commands:
        at = bisect_left(science_times, cmd.time)
        for neighbor in (at - 1, at):
            if 0 <= neighbor < len(science_times):
                if abs((cmd.time - science_times[neighbor]).total_seconds()) <= CONFLICT_TOLERANCE_S:
                    conflicts.append(
                        {
                            "science_utc": fmt(science_times[neighbor]),
                            "science_command": commands[neighbor].name,
                            "telecom_utc": fmt(cmd.time),
                            "telecom_command": cmd.name,
                        }
                    )
                    break

    # Cluster telecom commands into activity blocks and flag those matching no contact.
    uncorrelated = []
    if contacts and telecom_commands:
        cluster_start = cluster_stop = telecom_commands[0].time
        clusters = []
        for cmd in telecom_commands[1:]:
            if (cmd.time - cluster_stop).total_seconds() > TELECOM_BLOCK_GAP_S:
                clusters.append((cluster_start, cluster_stop))
                cluster_start = cmd.time
            cluster_stop = cmd.time
        clusters.append((cluster_start, cluster_stop))
        uncorrelated = [
            {"start_utc": fmt(start), "stop_utc": fmt(stop)}
            for start, stop in clusters
            if not any(overlap_s(start, stop, c["start"], c["stop"]) > 0 for c in contacts)
        ]

    return {"command_count": len(telecom_commands), "conflicts": conflicts, "uncorrelated_blocks": uncorrelated}


def compare_sequence(
    calendar: CalendarRecord,
    seq_path,
    contacts_path=None,
    telecom_path=None,
    end_buffer_s=DEFAULT_END_BUFFER_S,
    start_tolerance_s=0.0,
) -> dict:
    """Compare a generated sequence against a calendar's active observations.

    Pure comparison: writes nothing. Superseded observations are excluded.

    Parameters
    ----------
    calendar : CalendarRecord
        The calendar the sequence was generated from.
    seq_path : str or Path
        The ``S<yy>W<ww>.seq.json`` science sequence.
    contacts_path : str or Path, optional
        A ``ksat_contacts.json``; enables contact overlap annotation.
    telecom_path : str or Path, optional
        A ``T<yy>W<ww>.seq.json`` telecom sequence; enables the science/telecom collision
        and contact correlation cross-checks.
    end_buffer_s : float
        Seconds the closing PAYLOAD_READ precedes the true end of an observation.
    start_tolerance_s : float
        Seconds a block may start late before it counts as truncation.

    Returns
    -------
    dict
        The structured comparison, shaped like the sequence record's ``sequence``,
        ``observations``, ``unmatched_blocks``, and ``telecom`` fields.
    """
    seq_path = Path(seq_path)
    commands, use_cosmos = load_commands(seq_path)
    blocks = detect_blocks(commands)
    contacts = load_contacts(contacts_path) if contacts_path else []

    active = [obs for obs in calendar.observations if not obs.superseded]
    windows = [(to_utc(obs.requested.start_utc), to_utc(obs.requested.stop_utc)) for obs in active]
    assigned, unassigned = match_blocks(active, windows, blocks, end_buffer_s)

    observations = []
    for i, obs in enumerate(active):
        start, stop = windows[i]
        matched = [blocks[j] for j in assigned.get(i, [])]
        covered, truncation = _coverage_and_truncation(start, stop, matched, end_buffer_s, start_tolerance_s)
        mismatches, notes = _block_findings(obs, matched)
        if not matched:
            status = "dropped"
        else:
            status = "truncated" if sum(truncation.values()) > 0 else "scheduled"
        observations.append(
            {
                "obs_id": obs.obs_id,
                "target": obs.target,
                "priority": obs.priority,
                "start_utc": obs.requested.start_utc,
                "stop_utc": obs.requested.stop_utc,
                "requested_s": (stop - start).total_seconds(),
                "scheduled_status": status,
                "coverage_s": covered,
                "matched_blocks": [
                    {
                        "start_utc": fmt(b.start),
                        "stop_utc": fmt(b.effective_stop(end_buffer_s)),
                        "science_file": b.science_file,
                    }
                    for b in matched
                ],
                "truncation": truncation,
                "payload_mismatches": mismatches,
                "notes": notes,
                "contact_overlaps": [
                    {
                        "ground_station": c["ground_station"],
                        "antenna": c["antenna"],
                        "start_utc": fmt(c["start"]),
                        "stop_utc": fmt(c["stop"]),
                        "overlap_s": round(overlap_s(start, stop, c["start"], c["stop"]), 3),
                    }
                    for c in contacts
                    if overlap_s(start, stop, c["start"], c["stop"]) > 0
                ],
            }
        )

    match = SEQ_FILE_RE.search(seq_path.name)
    result = {
        "sequence": {
            "sequence_file_id": match["id"] if match else seq_path.name.split(".")[0],
            "calendar_id": calendar.calendar.calendar_id,
            "revision": calendar.calendar.revision,
            "command_count": len(commands),
            "use_cosmos": use_cosmos,
            "end_buffer_s": end_buffer_s,
            "start_tolerance_s": start_tolerance_s,
            "n_generated_blocks": len(blocks),
            "n_matched_blocks": len(blocks) - len(unassigned),
        },
        "observations": observations,
        "unmatched_blocks": [
            {
                "start_utc": fmt(blocks[j].start),
                "stop_utc": fmt(blocks[j].effective_stop(end_buffer_s)),
                "target": blocks[j].target or None,
                "science_file": blocks[j].science_file or None,
            }
            for j in unassigned
        ],
        "telecom": None,
    }

    if telecom_path is not None:
        result["telecom"] = _telecom_checks(commands, telecom_path, contacts)

    return result


def format_report(result: dict, priority_level=1) -> str:
    """Render the structured comparison as the text validation report.

    Layout follows the MOCSeqGen ``comparison_report.txt``: header counts and configuration,
    truncation summary, per-observation truncation, chronological findings, then telecom and
    contact findings. Summary lists are ordered priority descending (2 is most important).
    """

    def label(entry):
        parts = entry["obs_id"].split(":")
        return (
            f"visit={parts[-2][1:]} obs_id={parts[-1][1:]} target={entry['target']} priority={entry['priority']}"
        )

    seq = result["sequence"]
    observations = result["observations"]
    reported = [o for o in observations if o["priority"] >= priority_level]
    dropped = [o for o in reported if o["scheduled_status"] == "dropped"]
    truncated = [o for o in reported if o["scheduled_status"] == "truncated"]
    split = [o for o in reported if len(o["matched_blocks"]) > 1]

    lines = [
        "# Calendar vs Sequence Comparison Report",
        "",
        f"Sequence: {seq['sequence_file_id']} vs calendar {seq['calendar_id']} R{seq['revision']:03d}",
        f"Requested observations (all priorities): {len(observations)}",
        f"Requested observations (priority >= {priority_level}): {len(reported)}",
        f"Generated observation blocks: {seq['n_generated_blocks']}",
        f"Generated blocks matched to a request: {seq['n_matched_blocks']}",
        f"PAYLOAD_READ end buffer: {seq['end_buffer_s']:g} s",
        f"Start tolerance: {seq['start_tolerance_s']:g} s",
        "",
        "# Truncation summary",
        f"Dropped observations (priority >= {priority_level}): {len(dropped)}"
        f" ({sum(o['requested_s'] for o in dropped) / 60.0:.2f} min)",
        f"Split observations (priority >= {priority_level}, >1 generated block): {len(split)}",
        f"Truncated observations (priority >= {priority_level}): {len(truncated)} of {len(reported)}",
        f"Total minutes truncated: {sum(sum(o['truncation'].values()) for o in truncated) / 60.0:.2f}",
    ]
    if truncated:
        worst = max(truncated, key=lambda o: sum(o["truncation"].values()))
        lines.append(f"Worst truncation: {sum(worst['truncation'].values()) / 60.0:.2f} min ({label(worst)})")
        lines += ["", "Per-observation truncation (minutes), priority descending:"]
        for o in sorted(truncated, key=lambda o: (-o["priority"], o["start_utc"])):
            t = o["truncation"]
            lines.append(
                f"  {o['start_utc']} {label(o)} truncated={sum(t.values()) / 60.0:.2f} "
                f"(start={t['start_late_s'] / 60.0:.2f}, gap={t['mid_gap_s'] / 60.0:.2f}, end={t['end_early_s'] / 60.0:.2f})"
            )

    lines += ["", "# Findings (chronological UTC)"]
    findings = []
    for o in reported:
        t = o["truncation"]
        if o["scheduled_status"] == "dropped":
            findings.append(
                (o["start_utc"], f"{o['start_utc']} DROPPED {label(o)} lost={o['requested_s'] / 60.0:.2f} min")
            )
        elif o["scheduled_status"] == "truncated":
            first, last = o["matched_blocks"][0], o["matched_blocks"][-1]
            findings.append(
                (
                    o["start_utc"],
                    f"{o['start_utc']} TRUNCATED {label(o)} requested={o['start_utc']}..{o['stop_utc']} "
                    f"generated={first['start_utc']}..{last['stop_utc']} blocks={len(o['matched_blocks'])} "
                    f"truncated={sum(t.values()) / 60.0:.2f} min (start_late={t['start_late_s'] / 60.0:.2f} min, "
                    f"gap={t['mid_gap_s'] / 60.0:.2f} min, end_early={t['end_early_s'] / 60.0:.2f} min)",
                )
            )
        for note in o["notes"]:
            findings.append((o["start_utc"], f"{o['start_utc']} NOTE {label(o)} {note}"))
        for mismatch in o["payload_mismatches"]:
            findings.append(
                (
                    o["start_utc"],
                    f"{o['start_utc']} PARAM_MISMATCH {label(o)} {mismatch['parameter']}: "
                    f"requested={mismatch['requested']} generated={mismatch['scheduled']}",
                )
            )
    for block in result["unmatched_blocks"]:
        findings.append(
            (
                block["start_utc"],
                f"{block['start_utc']} ADDED_OBSERVATION target={block['target'] or '?'} "
                f"window={block['start_utc']}..{block['stop_utc']}",
            )
        )
    findings.sort()
    lines += [text for _, text in findings] or ["No discrepancies found."]

    telecom = result["telecom"]
    if telecom is not None:
        lines += [
            "",
            "# Telecom cross-checks",
            f"Telecom commands: {telecom['command_count']}",
            f"Science/telecom conflicts: {len(telecom['conflicts'])} (must be 0)",
        ]
        for conflict in telecom["conflicts"]:
            lines.append(
                f"  CONFLICT telecom {conflict['telecom_command']} at {conflict['telecom_utc']} collides with "
                f"science {conflict['science_command']} at {conflict['science_utc']}"
            )
        lines.append(f"Telecom activity blocks matching no contact: {len(telecom['uncorrelated_blocks'])}")
        for block in telecom["uncorrelated_blocks"]:
            lines.append(f"  UNCORRELATED {block['start_utc']}..{block['stop_utc']}")

    return "\n".join(lines) + "\n"


def _resolve_calendar(calendar, data_dir=None) -> CalendarRecord:
    """Accept a CalendarRecord, a calendar XML path, or an ingested calendar_id."""
    if isinstance(calendar, CalendarRecord):
        return calendar
    text = str(calendar)
    if text.lower().endswith(".xml"):
        return parse_calendar(text)
    db = ObservationDatabase(data_dir)
    for _, record in db.iter_records("calendars"):
        if record["calendar"]["calendar_id"] == text and not record["calendar"]["superseded"]:
            return CalendarRecord.from_dict(record)
    raise FileNotFoundError(f"No active ingested calendar with id {text!r}.")


def _emit_report(result, seq_path, priority_level) -> Path:
    """Write the text report beside the sequence file and print its summary to the console."""
    report = format_report(result, priority_level)
    report_path = Path(seq_path).with_name(f"{result['sequence']['sequence_file_id']}_validation_report.txt")
    report_path.write_text(report, encoding="utf-8")
    print(report.split("# Findings", 1)[0].rstrip())
    print(f"\nFull report: {report_path}")
    return report_path


def validate_sequence(
    seq_path,
    calendar,
    data_dir=None,
    contacts_path=None,
    telecom_path=None,
    end_buffer_s=DEFAULT_END_BUFFER_S,
    start_tolerance_s=0.0,
    priority_level=1,
) -> dict:
    """Validate a (draft) MOC sequence against a calendar. Writes nothing under ``data/``.

    Emits the validation report beside the sequence file and prints the summary, for the
    pushback loop with the MOC. Run it on as many drafts as needed; only the final sequence
    is ingested.

    Parameters
    ----------
    seq_path : str or Path
        The ``S<yy>W<ww>.seq.json`` sequence to validate.
    calendar : CalendarRecord, str, or Path
        The calendar to compare against: a record, an XML delivery path, or an ingested
        calendar_id.
    data_dir : str or Path, optional
        Only needed when ``calendar`` is a calendar_id.
    contacts_path, telecom_path : str or Path, optional
        Optional KSAT contacts and telecom sequence cross-check inputs.
    end_buffer_s, start_tolerance_s : float
        Comparison tuning, see `compare_sequence`.
    priority_level : int
        Report only observations with priority >= this (0 is lowest, 2 highest).

    Returns
    -------
    dict
        The structured comparison result.
    """
    record = _resolve_calendar(calendar, data_dir)
    result = compare_sequence(record, seq_path, contacts_path, telecom_path, end_buffer_s, start_tolerance_s)
    _emit_report(result, seq_path, priority_level)
    return result


def ingest_sequence(
    seq_path,
    calendar,
    data_dir=None,
    contacts_path=None,
    telecom_path=None,
    end_buffer_s=DEFAULT_END_BUFFER_S,
    start_tolerance_s=0.0,
    priority_level=1,
) -> Path:
    """Ingest the final MOC sequence: record it and update the calendar's observations.

    Runs the same comparison as `validate_sequence`, writes the sequence record under
    ``data/sequences/``, and fills each matched observation's ``scheduled`` block and status
    (``SCHEDULED``/``TRUNCATED``/``DROPPED``) in the stored calendar record. Re-ingesting an
    unchanged sequence file is a no-op.

    Parameters
    ----------
    seq_path : str or Path
        The final ``S<yy>W<ww>.seq.json``.
    calendar : str or Path
        The calendar this sequence realizes: an ingested calendar_id, or a delivery XML
        path (its id is looked up; the stored record is what gets updated).
    data_dir : str or Path, optional
        Explicit data directory; discovered when omitted.
    contacts_path, telecom_path, end_buffer_s, start_tolerance_s, priority_level
        As in `validate_sequence`.

    Returns
    -------
    Path
        The sequence record file.
    """
    seq_path = Path(seq_path)
    db = ObservationDatabase(data_dir)

    digest = sha256_of_file(seq_path)
    for path, record in db.iter_records("sequences"):
        if record["source"]["sha256"] == digest:
            logger.info(f"{seq_path.name} already ingested (unchanged content); nothing to do.")
            return path

    # Ingest always compares against (and updates) the stored active calendar record.
    text = str(calendar)
    if text.lower().endswith(".xml"):
        match = DELIVERY_RE.match(Path(text).name)
        if match is None:
            raise ValueError(f"{Path(text).name} does not look like a calendar delivery.")
        text = match["calendar_id"]
    calendar_record = _resolve_calendar(text, data_dir)

    result = compare_sequence(calendar_record, seq_path, contacts_path, telecom_path, end_buffer_s, start_tolerance_s)
    if result["telecom"] is not None and result["telecom"]["conflicts"]:
        logger.warning(f"{seq_path.name}: {len(result['telecom']['conflicts'])} science/telecom time conflicts recorded.")

    record = {
        "schema_version": SEQUENCE_RECORD_VERSION,
        "ingested_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"path": str(seq_path.resolve()), "sha256": digest},
        "contacts_source": (
            {"path": str(Path(contacts_path).resolve()), "sha256": sha256_of_file(contacts_path)} if contacts_path else None
        ),
        "telecom_source": (
            {"path": str(Path(telecom_path).resolve()), "sha256": sha256_of_file(telecom_path)} if telecom_path else None
        ),
        **result,
    }
    record_path = db.write_record("sequences", seq_path.name, record)

    # Fill the scheduled block and status of every active observation in the calendar record.
    by_obs_id = {entry["obs_id"]: entry for entry in result["observations"]}
    calendar_name = f"{calendar_record.calendar.calendar_id}-R{calendar_record.calendar.revision:03d}.json"
    stored = db.read_record("calendars", calendar_name)
    for obs in stored["observations"]:
        entry = by_obs_id.get(obs["obs_id"])
        if entry is None:
            continue
        obs["scheduled"] = {
            "sequence_file_id": result["sequence"]["sequence_file_id"],
            "scheduled_status": entry["scheduled_status"],
            "coverage_s": entry["coverage_s"],
            "truncation": entry["truncation"],
            "matched_blocks": entry["matched_blocks"],
            "payload_mismatches": entry["payload_mismatches"],
            "contact_overlaps": entry["contact_overlaps"],
            "notes": entry["notes"],
        }
        obs["status"] = entry["scheduled_status"].upper()
    db.write_record("calendars", calendar_name, stored)

    n_scheduled = sum(1 for o in result["observations"] if o["scheduled_status"] == "scheduled")
    n_truncated = sum(1 for o in result["observations"] if o["scheduled_status"] == "truncated")
    n_dropped = sum(1 for o in result["observations"] if o["scheduled_status"] == "dropped")
    logger.info(
        f"Ingested {seq_path.name}: {n_scheduled} scheduled, {n_truncated} truncated, {n_dropped} dropped "
        f"against {calendar_name}."
    )
    _emit_report(result, seq_path, priority_level)
    return record_path
