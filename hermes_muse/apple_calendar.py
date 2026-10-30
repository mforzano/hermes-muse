"""Pure adaptation of Apple PIM EventKit receipts, without connector I/O.

Import calendar helpers lazily to avoid a circular module initialization.
"""
import copy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LIMITATIONS = [
    "Apple PIM observed instances only: source timezone and original all-day dates unavailable; device dates are display dates",
    "Apple PIM original recurrence identity, moved/exception flags and cancellation history unavailable; no cross-read instance reconciliation",
    "Apple PIM account identity unavailable; account denotes the local EventKit store, not a provider account",
    "Apple PIM busy/transparency and attendance status unavailable; shown events do not establish availability or confirmed attendance",
]
# EventKit fields that a full receipt carries on every event (all-day events add allDayStart/allDayEnd).
FULL_FIELDS = ("occurrenceDate", "isDetached", "isRecurring", "status", "availability")
# Known with full receipts, but not a reason for partial coverage.
FULL_NOTES = [
    "Apple PIM deleted recurring instances (EXDATE) are not reported; an absent occurrence is not proof of cancellation",
    "Apple PIM attendee response of the current user and iCalendar UID are not reported; busy follows EventKit availability",
]
MIXED_GAP = ("Some Apple PIM events lack full EventKit fields (occurrenceDate/status/availability/timeZone/allDay dates); "
             "read events without a fields filter so every event carries them")
STATUS = {"canceled": "cancelled", "tentative": "tentative", "confirmed": "confirmed"}


def busy_fields(raw):
    """Busy from EventKit availability; without it, keep the observed unknown default."""
    availability = raw.get("availability")
    if availability in ("busy", "unavailable"):
        return {"busy": True, "busy_basis": "availability:" + availability}
    if availability == "tentative":
        return {"busy": True, "busy_basis": "availability:tentative"}
    if availability == "free":
        return {"busy": False, "busy_basis": "availability:free"}
    return None


def full_event(raw, start, end, device, zone):
    """Strict-contract event from a receipt carrying the full EventKit fields, or None to observe it."""
    if any(field not in raw for field in FULL_FIELDS) or not isinstance(raw["occurrenceDate"], str):
        return None
    if type(raw["isDetached"]) is not bool or type(raw["isRecurring"]) is not bool:
        raise ValueError("Apple isDetached/isRecurring must be booleans")
    recurring = raw["isRecurring"] or raw["isDetached"] or bool(raw.get("recurrence"))
    event = {"id": raw["id"], "title": raw["title"], "all_day": raw["isAllDay"], "recurring": recurring,
             "original": copy.deepcopy(raw)}
    if raw.get("timeZone"):
        try:
            ZoneInfo(raw["timeZone"])
        except (ValueError, KeyError):
            return None  # Non-IANA identifier (for example GMT+0800): keep the observed device basis.
        event.update(timezone=raw["timeZone"], timezone_basis="event")
    else:
        # Floating event: EventKit shows it at the same wall time on the reading device.
        event.update({key: device[key] for key in ("timezone", "timezone_kind", "utc_offset_seconds") if key in device},
                     timezone_basis="floating-device")
    if raw["isAllDay"]:
        if "allDayStart" not in raw or "allDayEnd" not in raw:
            return None
        first, last = date.fromisoformat(raw["allDayStart"]), date.fromisoformat(raw["allDayEnd"])
        event_zone = ZoneInfo(raw["timeZone"]) if raw.get("timeZone") else zone
        local_end = end.astimezone(event_zone)
        # EventKit ends an all-day event at 23:59:59 of its last day; Muse dates are end-exclusive.
        if (local_end.hour, local_end.minute, local_end.second) != (0, 0, 0) or last <= first:
            last += timedelta(days=1)
        event.update(start=first.isoformat(), end=last.isoformat())
    else:
        event.update(start=raw["startDate"], end=raw["endDate"])
    if recurring:
        from .calendar import instant
        instant(raw["occurrenceDate"])
        event["occurrence_id"] = raw["occurrenceDate"]  # Original occurrence start, kept by moved instances.
    if raw["status"] in STATUS:
        event["status"] = STATUS[raw["status"]]
    busy = busy_fields(raw)
    if busy:
        event.update(busy)
    else:
        event["busy_basis"] = "availability:" + str(raw["availability"]) + "; default timed-busy/all-day-free"
    return event


def adapt(source):
    from .calendar import instant, required_text, resolve_zone
    result = copy.deepcopy(source)
    device = result['device_timezone']
    zone = resolve_zone(device)  # Explicit receipt from the connector host, never the profile by assumption.
    capable = any('sourceId' in resource for page in result['inventory'].get('pages', []) for resource in page['items'])
    full = observed = 0
    for page in result['inventory'].get('pages', []):
        for resource in page['items']:
            resource['display_timezone'] = copy.deepcopy(device)
    for read in result.get('calendars', []):
        for page in read.get('pages', []):
            normalized = []
            for raw in page['items']:
                try:
                    required_text(raw, 'id')
                    required_text(raw, 'title')
                    if raw['calendarId'] != read['resource_id']:
                        raise ValueError('Apple event calendarId differs from queried resource')
                    if type(raw['isAllDay']) is not bool:
                        raise ValueError('Apple isAllDay must be a boolean')
                    if 'recurrence' in raw and not isinstance(raw['recurrence'], list):
                        raise ValueError('Apple recurrence must be a list of rules')
                    start, end = instant(raw['startDate']), instant(raw['endDate'])
                    # localStart/localEnd are en_US_POSIX device display strings, minute precision.
                    for field, at in (('localStart', start), ('localEnd', end)):
                        local = datetime.strptime(raw[field], '%Y-%m-%d %I:%M %p')
                        if local != at.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0):
                            raise ValueError('Apple local/UTC times disagree with explicit device timezone')
                    event = full_event(raw, start, end, device, zone)
                    if event is None:
                        observed += 1
                        event = {'id': raw['id'], 'title': raw['title'],
                                 'start': raw['startDate'], 'end': raw['endDate'],
                                 'all_day': raw['isAllDay'], 'recurring': bool(raw.get('recurrence')),
                                 'time_basis': 'apple-pim-observed',
                                 'identity_basis': 'native-id-and-observed-interval',
                                 'recurrence_identity': 'unavailable', 'status': 'observed',
                                 'recurrence_basis': 'rule-present' if raw.get('recurrence') else 'not-reported',
                                 'original': copy.deepcopy(raw)}
                        # Availability/attendance is not exposed: do not invent confirmed or busy.
                        event['busy'] = False
                        event['busy_basis'] = 'unavailable'
                        event.update(busy_fields(raw) or {})
                        if raw.get('status') == 'canceled':
                            event['status'] = 'cancelled'  # Never a live commitment, even when other fields are missing.
                        if 'preparation' in raw:
                            # Optional Muse judgment annotation, not an EventKit property.
                            event['preparation'] = copy.deepcopy(raw['preparation'])
                        if raw['isAllDay']:
                            event['display_all_day_dates'] = {
                                'start': start.astimezone(zone).date().isoformat(),
                                'end_exclusive': end.astimezone(zone).date().isoformat(),
                                'timezone': copy.deepcopy(device), 'basis': 'device-display'}
                    else:
                        full += 1
                        if 'preparation' in raw:
                            event['preparation'] = copy.deepcopy(raw['preparation'])
                    normalized.append(event)
                except (KeyError, TypeError, ValueError) as exc:
                    page['status'] = 'partial'
                    page['error'] = 'Invalid Apple observation: ' + str(exc)
            page['items'] = normalized
    if not observed and (full or capable):
        result['capability_gaps'] = []
        result['known_limitations'] = list(FULL_NOTES)
    else:
        result['capability_gaps'] = list(LIMITATIONS) + ([MIXED_GAP] if full else [])
    if device.get('timezone_kind', 'iana') == 'fixed':
        result['capability_gaps'].append('Apple PIM device timezone is a fixed offset; DST transitions unavailable')
    return result
