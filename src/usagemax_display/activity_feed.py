"""Readable activity derived exclusively from observed telemetry records."""
import re
from dataclasses import dataclass

LANES = (
    ('thinking', 'Thinking', '#DD78FF'),
    ('tools', 'Tools', '#00E8FF'),
    ('output', 'Output', '#C6FF00'),
    ('updates', 'Updates', '#FF38A8'),
)


def describe(record):
    """Translate common event conventions; retain unfamiliar harness actions."""
    words = re.sub(r'[^a-zA-Z0-9]+', ' ', record.name).strip()
    upper = words.upper()
    if record.status in {'ERR', 'WRN'}:
        return 'alerts', 'Error reported' if record.status == 'ERR' else 'Warning reported', '#FF647C'
    finished = any(word in upper for word in ('DONE', 'COMPLETE', 'FINISH', 'RETURN'))
    if any(word in upper for word in ('TOOL', 'COMMAND', 'EXECUTION')):
        label = 'Tool completed' if finished or 'OUTPUT' in upper else 'Tool invoked'
        # Known-safe function identifiers, never arguments or tool output.
        segments = [part.strip() for part in record.name.split('/')]
        if len(segments) >= 3 and segments[-2].upper() == 'TOOL' and segments[-1].upper() != 'OUTPUT':
            name = segments[-1].split('.')[-1].replace('_', ' ').lower()
            label = name.capitalize() if name else label
        return 'tools', label, '#00E8FF'
    if 'REASON' in upper or 'THINK' in upper:
        return 'thinking', 'Reasoning finished' if finished else 'Reasoning', '#DD78FF'
    if any(word in upper for word in ('TOKEN', 'USAGE', 'COUNTER')):
        return 'updates', 'Usage measured', '#FF38A8'
    if 'TASK COMPLETE' in upper or 'TURN COMPLETE' in upper:
        return 'output', 'Turn completed', '#C6FF00'
    if 'RESPONSE' in upper or 'AGENT MESSAGE' in upper or 'COMPLETION' in upper:
        return 'output', 'Response ready' if finished else 'Response streaming', '#C6FF00'
    if 'USER MESSAGE' in upper or 'PROMPT' in upper:
        return 'updates', 'Prompt received', '#FFAA56'
    if 'TURN' in upper and 'CONTEXT' not in upper:
        return 'updates', 'Turn started', '#FF38A8'
    # Strip only the exact service prefix, keeping unknown providers legible.
    prefix = re.sub(r'[^a-zA-Z0-9]+', ' ', record.service).strip()
    if upper.startswith(prefix.upper()+' '):
        words = words[len(prefix):].strip()
    if words.upper().startswith('EVENT '):
        words = words[6:]
    return 'updates', words.capitalize() or 'Agent event', '#90A5C3'


@dataclass(frozen=True)
class ActivityGroup:
    timestamp: float
    category: str
    title: str
    color: str
    service: str
    host: str
    model: str
    session_id: str
    count: int
    duration_ms: int | None


class ActivityFeed:
    """Bounded source-time window; repeated polls never create activity."""
    def __init__(self):
        self.records = ()
        self.groups = ()
        self.lanes = {key: () for key, _, _ in LANES}
        self.count = 0
        self.errors = 0
        self.latest_at = 0.0
        self.latest_change = 0.0
        self._fingerprint = None

    def update(self, records, now):
        # Recompute at most four times a second; motion uses timestamps at the
        # native render cadence, independently of this aggregation.
        fingerprint = (int(now*4), len(records), records[-1].identity if records else None)
        if fingerprint == self._fingerprint:
            return
        self._fingerprint = fingerprint
        unique = {r.identity: r for r in records if 0 <= now-r.timestamp < 300}
        rows = sorted(unique.values(), key=lambda r:r.timestamp)
        self.records = tuple(rows)
        latest = rows[-1].timestamp if rows else 0
        if latest > self.latest_at:
            self.latest_change = now
        self.latest_at = latest
        recent = [r for r in rows if now-r.timestamp < 60]
        self.count = len(recent)
        self.errors = sum(r.status == 'ERR' for r in recent)
        lanes = {key: [] for key, _, _ in LANES}
        grouped = {}
        for record in rows:
            category, title, color = describe(record)
            if now-record.timestamp < 60:
                lanes[category if category in lanes else 'updates'].append(record.timestamp)
            key = (record.host, record.service, record.model, record.session_id, category, title)
            prior = grouped.get(key)
            # A burst groups repeated actions for at most 15 seconds; identical
            # actions later on remain separately visible.
            count = prior.count+1 if prior and record.timestamp-prior.timestamp <= 15 else 1
            grouped[key] = ActivityGroup(record.timestamp, category, title, color, record.service,
                record.host, record.model, record.session_id, count, record.duration_ms)
        self.lanes = {key: tuple(value) for key, value in lanes.items()}
        self.groups = tuple(sorted(grouped.values(), key=lambda g:g.timestamp, reverse=True))

    def visible_groups(self, now, count=4):
        # Recent errors remain visible even during a heavy stream of updates.
        alerts = [g for g in self.groups if g.category == 'alerts' and now-g.timestamp < 60]
        bookkeeping = {'Thread settings applied', 'World state', 'Session meta', 'Context updated'}
        others = [g for g in self.groups if g not in alerts and g.title not in bookkeeping]
        # A burst of reasoning deltas should not crowd out the latest tool,
        # response or usage update. Keep one recent action per lane first.
        selected, deferred, categories = list(alerts), [], {'alerts'}
        for group in others:
            if group.category in categories:
                deferred.append(group)
            else:
                selected.append(group)
                categories.add(group.category)
        return tuple((selected+deferred)[:count])
