"""List Ring devices and their recent history, filtered with ``event_types``.

Live (Developer Playground token, valid about 30 minutes):

    export RING_ACCESS_TOKEN=...
    python list_devices_history.py --types motion,ding,on_demand

Offline, against fixtures recorded with ``ringpartner capture-fixtures`` (no token, no network):

    python list_devices_history.py --replay ../../../fixtures/playground

Notes
-----
* History filters are ``event_types`` (plural, comma-separated, composite values such as
  ``motion.human``). The singular ``event_type`` is silently ignored by the API.
* ``iter_history`` follows ``links.next`` pagination for you and stops on the first empty page.
* Playground button presses show up as ``on_demand`` events.
* Replay mode: when the exact query (here, the ``event_types`` filter) was not recorded, the
  replay transport serves the unfiltered recording of the same path, so the filter is not
  applied offline.
"""

from __future__ import annotations

import argparse
import os
import sys

from ringpartner import ReplayTransport, RingClient


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--types", default="motion,ding,on_demand",
                    help="comma-separated event_types filter (default: %(default)s)")
    ap.add_argument("--limit", type=int, default=10, help="max events per device")
    ap.add_argument("--replay", metavar="DIR", help="serve responses from a fixture directory")
    args = ap.parse_args()

    if args.replay:
        token, transport = "unused", ReplayTransport(args.replay)
    else:
        token, transport = os.environ.get("RING_ACCESS_TOKEN"), None
        if not token:
            print("set RING_ACCESS_TOKEN or pass --replay DIR", file=sys.stderr)
            return 2
    types = [t.strip() for t in args.types.split(",") if t.strip()]

    with RingClient(token, transport=transport) as ring:
        devices = ring.devices()  # ?include=status,capabilities,configurations,location
        for d in devices:
            print(f"{d.name!r}  online={d.online}  encrypted={d.is_encrypted}  chime={d.is_chime}")
            print(f"  id={d.id[:32]}...")
            if d.is_encrypted:
                print("  end-to-end encrypted: media cannot be analysed, skipping history")
                continue
            shown = _print_events(ring.iter_history(d.id, event_types=types), args.limit)
            if not shown:
                print("  no matching events")
    return 0


def _print_events(events, limit: int) -> int:
    n = 0
    for ev in events:
        print(f"  {ev.start:%Y-%m-%d %H:%M:%S %Z}  {ev.event_type:<12}"
              f"  third_party_reviewed={ev.is_third_party_reviewed}")
        n += 1
        if n >= limit:
            break
    return n


if __name__ == "__main__":
    sys.exit(main())
