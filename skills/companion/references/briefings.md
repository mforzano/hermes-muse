# Briefings

A briefing the user asked for (daily or on demand) is built from reads:
- `read(kind=status)` for health and open counts;
- `read(kind=goal)` for goals, and `read(kind=goal, id)` for one that needs detail: waiting on the user, blocked,
  past its next check, or changed today;
- `read(kind=notice)` for what Muse told the user recently or holds for later;
- `read(kind=finding)` for open review findings;
- `calendar_view` for the coming days.

Lead with what needs the user's decision today, then what changed, then what is coming. A partial calendar or failing
source is stated as a gap, never as "all clear". A briefing reports; it does not create goals, notices or watches.
