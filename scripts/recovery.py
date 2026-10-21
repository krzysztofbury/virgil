"""Create, verify, or restore a complete offline recovery bundle, or open an off-site copy.

Stop every Virgil process before creating a bundle. Restore only to a new data
directory, then point the stopped application at it. Existing data is never replaced.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyrage import x25519  # noqa: E402

from app.recovery import create, restore, unseal, verify  # noqa: E402


def keygen(identity_file: Path) -> str:
    """Write a new age identity, readable only by its owner, and return its public key."""
    identity = x25519.Identity.generate()
    descriptor = os.open(identity_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(f"# public key: {identity.to_public()}\n{identity}\n")
    return str(identity.to_public())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("create")
    backup.add_argument("--central", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    backup.add_argument("--key-file", type=Path)
    backup.add_argument("--stopped", action="store_true", required=True, help="Confirm all Virgil writers are stopped")
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    recovery = commands.add_parser("restore")
    recovery.add_argument("bundle", type=Path)
    recovery.add_argument("--destination", type=Path, required=True)
    opened = commands.add_parser("unseal", help="Decrypt a downloaded off-site copy into a bundle directory")
    opened.add_argument("copy", type=Path, help="Directory holding one run's downloaded files")
    opened.add_argument("--identity", type=Path, required=True, help="age identity file holding the secret key")
    opened.add_argument("--destination", type=Path, required=True)
    keys = commands.add_parser("keygen", help="Create the key pair off-site copies are encrypted to")
    keys.add_argument("--identity-file", type=Path, required=True, help="New file for the secret key")
    args = parser.parse_args()
    try:
        if args.command == "create":
            create(args.central, args.destination, args.key_file)
        elif args.command == "restore":
            restore(args.bundle, args.destination)
        elif args.command == "unseal":
            unseal(args.copy, args.identity, args.destination)
        elif args.command == "keygen":
            print(f"Public key (VIRGIL_OFFSITE_RECIPIENT): {keygen(args.identity_file)}")
            print(f"Secret key written to {args.identity_file}. Keep it off this machine; it opens every copy.")
            return
        else:
            verify(args.bundle)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        parser.exit(1, f"Recovery refused: {exc}\n")
    print(f"Recovery {args.command} complete.")


if __name__ == "__main__":
    main()
