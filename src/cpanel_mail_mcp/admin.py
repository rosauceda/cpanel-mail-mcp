"""Admin subcommand: manage users in the multi-user users.json.

Invoked as `cpanel-mail-mcp admin <subcommand>`. Requires `EMAIL_USERS_FILE`
to be set so it knows where to write. Meant to run on the server host by
the operator (over SSH), not exposed as an HTTP endpoint.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import users as users_mod


def _shorten(token: str) -> str:
    if len(token) <= 12:
        return token
    return f"{token[:6]}…{token[-4:]}"


def _find_index(all_users: list[dict], email: str) -> int:
    for i, u in enumerate(all_users):
        if u.get("account", {}).get("user") == email:
            return i
    return -1


def cmd_add_user(args: argparse.Namespace) -> int:
    all_users = users_mod.load_users_raw()
    if _find_index(all_users, args.email) >= 0:
        print(
            f"user {args.email!r} already exists. "
            f"Use `rotate-token --email {args.email}` or `remove-user --email {args.email}` first.",
            file=sys.stderr,
        )
        return 1
    password = args.password or getpass.getpass(f"mailbox password for {args.email}: ")
    if not password:
        print("password cannot be empty.", file=sys.stderr)
        return 1
    if not args.imap_host and not args.host:
        print("either --host or --imap-host is required.", file=sys.stderr)
        return 1
    if not args.smtp_host and not args.host:
        print("either --host or --smtp-host is required.", file=sys.stderr)
        return 1

    token = users_mod.new_token()
    all_users.append(
        {
            "token": token,
            "account": {
                "name": args.name or args.email,
                "user": args.email,
                "password": password,
                "smtp_host": args.smtp_host or args.host,
                "smtp_port": args.smtp_port,
                "imap_host": args.imap_host or args.host,
                "imap_port": args.imap_port,
                "sent_folder": args.sent_folder,
                "drafts_folder": args.drafts_folder,
                "save_to_sent": args.save_to_sent,
                "from_name": args.from_name,
            },
        }
    )
    users_mod.save_users(all_users)
    print(f"added user: {args.email}")
    print()
    print("bearer token (SHARE ONCE — you can't recover it later):")
    print(f"  {token}")
    print()
    print("The user registers in their Claude Code with:")
    print(
        f'  claude mcp add --transport http --scope user cpanel-mail \\\n'
        f'    --header "Authorization: Bearer {token}" \\\n'
        f"    https://YOUR-HOSTNAME/mcp"
    )
    return 0


def cmd_list_users(_: argparse.Namespace) -> int:
    all_users = users_mod.load_users_raw()
    if not all_users:
        print("no users configured yet. Add one with `admin add-user --email … --host …`.")
        return 0
    print(f"{len(all_users)} user(s):")
    for u in all_users:
        acct = u.get("account", {})
        token = u.get("token", "")
        print(
            f"  {acct.get('user', '?'):40s}  token={_shorten(token)}  "
            f"imap={acct.get('imap_host', '?')}  smtp={acct.get('smtp_host', '?')}"
        )
    return 0


def cmd_remove_user(args: argparse.Namespace) -> int:
    all_users = users_mod.load_users_raw()
    idx = _find_index(all_users, args.email)
    if idx < 0:
        print(f"user {args.email!r} not found.", file=sys.stderr)
        return 1
    del all_users[idx]
    users_mod.save_users(all_users)
    print(f"removed {args.email}. Their bearer token is now revoked.")
    return 0


def cmd_rotate_token(args: argparse.Namespace) -> int:
    all_users = users_mod.load_users_raw()
    idx = _find_index(all_users, args.email)
    if idx < 0:
        print(f"user {args.email!r} not found.", file=sys.stderr)
        return 1
    token = users_mod.new_token()
    all_users[idx]["token"] = token
    users_mod.save_users(all_users)
    print(f"new token for {args.email}:")
    print(f"  {token}")
    print("(previous token is revoked immediately)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cpanel-mail-mcp admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add-user", help="register a new user + generate their bearer token")
    add.add_argument("--email", required=True, help="user's email address (also the IMAP login)")
    add.add_argument("--password", help="mailbox password (will prompt if omitted)")
    add.add_argument("--host", help="shared IMAP+SMTP host (used if per-protocol hosts aren't given)")
    add.add_argument("--imap-host", dest="imap_host")
    add.add_argument("--smtp-host", dest="smtp_host")
    add.add_argument("--imap-port", dest="imap_port", type=int, default=993)
    add.add_argument("--smtp-port", dest="smtp_port", type=int, default=465)
    add.add_argument("--sent-folder", dest="sent_folder", default="INBOX.Sent")
    add.add_argument("--drafts-folder", dest="drafts_folder", default="INBOX.Drafts")
    add.add_argument(
        "--no-save-to-sent",
        dest="save_to_sent",
        action="store_false",
        default=True,
        help="disable IMAP APPEND of outgoing mail to the Sent folder",
    )
    add.add_argument("--from-name", dest="from_name", help='display name in the "From:" header')
    add.add_argument("--name", help="friendly handle (defaults to the email)")
    add.set_defaults(func=cmd_add_user)

    ls = sub.add_parser("list-users", help="list all configured users")
    ls.set_defaults(func=cmd_list_users)

    rm = sub.add_parser("remove-user", help="revoke a user's access")
    rm.add_argument("--email", required=True)
    rm.set_defaults(func=cmd_remove_user)

    rot = sub.add_parser("rotate-token", help="issue a new bearer token, revoke the old one")
    rot.add_argument("--email", required=True)
    rot.set_defaults(func=cmd_rotate_token)

    return p


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
