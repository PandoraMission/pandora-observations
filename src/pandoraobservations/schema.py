"""Record dataclasses and JSON serialization for the observation database.

The authoritative field-by-field definitions live in ``docs/schemas/`` in this repo; the
classes here mirror those documents. Schema versions bump only on breaking changes, and
adding optional fields is not a breaking change.
"""

# Standard library
from dataclasses import asdict, dataclass, field
from enum import StrEnum

CALENDAR_RECORD_VERSION = 1


class ObservationStatus(StrEnum):
    """Lifecycle state of a single requested observation.

    Every downstream stage is optional and can arrive late, so any state is a normal
    resting point. Supersession is tracked by a separate ``superseded`` flag, not a status,
    because a superseded observation keeps the last state it reached.
    """

    REQUESTED = "REQUESTED"  # a calendar exists, nothing downstream seen yet
    SCHEDULED = "SCHEDULED"  # sequence ingest found it fully covered
    TRUNCATED = "TRUNCATED"  # sequence ingest found it partially covered
    DROPPED   = "DROPPED"    # sequence ingest found no coverage at all
    EXECUTED  = "EXECUTED"   # a report confirmed it ran, verdict not yet computed
    SUCCESS   = "SUCCESS"
    PARTIAL   = "PARTIAL"
    FAILED    = "FAILED"


@dataclass
class Source:
    """Provenance of one ingested delivery file."""

    path: str
    sha256: str


@dataclass
class RequestedWindow:
    """The window and pointing a calendar asked for."""

    start_utc: str
    stop_utc: str
    duration_s: float
    ra_deg: float
    dec_deg: float
    roll_deg: float
    pri_cmd_dir: int | None = None


@dataclass
class Observation:
    """One ``Observation_Sequence`` from a calendar: a single contiguous pointing.

    ``obs_id`` is provenance, not identity: visit and sequence ids are renumbered by the
    scheduler across calendar revisions, so downstream matching is always target plus time
    overlap (see ``docs/schemas/calendar-record.md``).
    """

    obs_id: str
    calendar_id: str
    revision: int
    visit_id: str
    sequence_id: str
    target: str
    target_key: str
    priority: int  # 0 is the lowest priority, 2 is currently the highest
    requested: RequestedWindow
    payload: dict
    status: ObservationStatus = ObservationStatus.REQUESTED
    superseded: bool = False
    scheduled: dict | None = None  # filled by sequence ingest
    executed: dict | None = None  # filled by report ingest
    quality: dict | None = None  # filled by report ingest

    @classmethod
    def from_dict(cls, data: dict) -> "Observation":
        data = dict(data)
        data["requested"] = RequestedWindow(**data["requested"])
        data["status"] = ObservationStatus(data["status"])
        return cls(**data)


@dataclass
class CalendarInfo:
    """Parsed calendar header plus the raw ``<Meta>`` element.

    ``calendar_status`` is recorded verbatim and never gates ingest. ``claimed_*`` are the
    scheduler's own totals and may disagree with ``parsed_*``; the disagreement is data,
    not an error. ``meta_raw`` holds every ``<Meta>`` attribute as strings so new scheduler
    versions can add attributes without a schema change here.
    """

    calendar_id: str
    revision: int
    valid_from: str
    expires: str
    created: str
    delivery_id: str
    calendar_status: str
    scheduler_version: str
    tle_line1: str
    tle_line2: str
    claimed_visits: int | None
    claimed_sequences: int | None
    parsed_visits: int
    parsed_sequences: int
    meta_raw: dict = field(default_factory=dict)
    superseded: bool = False


@dataclass
class CalendarRecord:
    """One ingested calendar delivery: the header plus every observation it requested."""

    ingested_utc: str
    source: Source
    calendar: CalendarInfo
    observations: list[Observation]
    schema_version: int = CALENDAR_RECORD_VERSION

    def to_dict(self) -> dict:
        # Explicit ordering so the JSON on disk reads header-first, like the schema doc.
        return {
            "schema_version": self.schema_version,
            "ingested_utc": self.ingested_utc,
            "source": asdict(self.source),
            "calendar": asdict(self.calendar),
            "observations": [asdict(obs) for obs in self.observations],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CalendarRecord":
        return cls(
            schema_version=data["schema_version"],
            ingested_utc=data["ingested_utc"],
            source=Source(**data["source"]),
            calendar=CalendarInfo(**data["calendar"]),
            observations=[Observation.from_dict(obs) for obs in data["observations"]],
        )
