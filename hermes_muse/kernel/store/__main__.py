"""python -m hermes_muse.kernel.store {migrate --from HOME | rollback-bridge --home HOME} [--dry-run]

Offline tools: stop the profile's Muse jobs (or work on a copy) and back up muse/ first.
"""
import argparse
import json

from .bridge import bridge
from .migrate import migrate

parser = argparse.ArgumentParser(prog="python -m hermes_muse.kernel.store", description=__doc__.split("\n\n")[1])
commands = parser.add_subparsers(dest="command", required=True)
command = commands.add_parser("migrate", help="0.3.1/0.3.16 state.db -> muse/state-v2.db")
command.add_argument("--from", dest="home", required=True, help="profile home that contains muse/state.db")
command.add_argument("--dry-run", action="store_true")
command = commands.add_parser("rollback-bridge", help="write v2 facts back into muse/state.db before reinstalling 0.3.x")
command.add_argument("--home", required=True)
command.add_argument("--dry-run", action="store_true")
args = parser.parse_args()
run = migrate if args.command == "migrate" else bridge
print(json.dumps(run(args.home, dry_run=args.dry_run), ensure_ascii=False, indent=2))
