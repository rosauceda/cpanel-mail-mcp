"""In-memory stand-in for `imaplib.IMAP4_SSL`, faithful to the reply shapes
imaplib hands back (tuples for literals, `[None]` for empty results, `NO`
with `[TRYCREATE]`, untagged `COPYUID`).

UIDs are deliberately different from sequence numbers so any code that mixes
the two picks the wrong message and fails the tests.
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import re
from dataclasses import dataclass, field


@dataclass
class Msg:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)


@dataclass
class Folder:
    name: str
    special: str = ""
    messages: list[Msg] = field(default_factory=list)
    uidnext: int = 1


class FakeServer:
    def __init__(self, *, supports_move: bool = True, uidplus: bool = True,
                 flags_after_literal: bool = False, utf8_search: bool = True) -> None:
        self.folders: dict[str, Folder] = {}
        self.supports_move = supports_move
        self.uidplus = uidplus
        self.flags_after_literal = flags_after_literal
        self.utf8_search = utf8_search
        self.commands: list[tuple] = []
        self.passwords: dict[str, str] = {}  # user → password; empty = accept anything
        self.add_folder("INBOX")

    def connect(self, account) -> "FakeIMAP":
        """Stand-in for `imap_ops._connect`: logs in, so passwords are checked."""
        m = FakeIMAP(self)
        m.login(account.user, account.password)
        return m

    def add_folder(self, name: str, special: str = "", uidnext: int = 1) -> Folder:
        f = Folder(name, special, uidnext=uidnext)
        self.folders[name] = f
        return f

    def add(self, folder: str, raw: bytes | str, uid: int | None = None,
            flags: set[str] | None = None) -> int:
        f = self.folders[folder]
        if isinstance(raw, str):
            raw = raw.replace("\n", "\r\n").encode("utf-8")
        uid = uid or f.uidnext
        f.messages.append(Msg(uid, raw, set(flags or ())))
        f.messages.sort(key=lambda m: m.uid)
        f.uidnext = max(f.uidnext, uid + 1)
        return uid

    def uids(self, folder: str) -> list[int]:
        return [m.uid for m in self.folders[folder].messages]

    def get(self, folder: str, uid: int) -> Msg | None:
        return next((m for m in self.folders[folder].messages if m.uid == uid), None)


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return re.sub(r'\\(.)', r'\1', s[1:-1])
    return s


def _headers_text(raw: bytes) -> str:
    return raw.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")


def _decoded_header(msg, name: str) -> str:
    from email.header import decode_header, make_header
    v = msg.get(name)
    return str(make_header(decode_header(v))) if v else ""


_LIT = "\x00"  # marks where a literal goes in a BODYSTRUCTURE being built


def _bodystructure(part, lits: list[bytes]) -> str:
    """RFC 3501 BODYSTRUCTURE for an email.message part. Non-ASCII strings are
    emitted as literals (as Dovecot does), collected in `lits`."""
    def q(v) -> str:
        if v is None:
            return "NIL"
        v = email.utils.collapse_rfc2231_value(v) if isinstance(v, tuple) else str(v)
        if not v.isascii():
            lits.append(v.encode("utf-8"))
            return _LIT
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def plist(pairs) -> str:
        return "(" + " ".join(f"{q(k)} {q(v)}" for k, v in pairs) + ")" if pairs else "NIL"

    if part.get_content_type() in ("message/rfc822",):
        inner = part.get_payload()[0]
        return (f'("message" "rfc822" NIL NIL NIL "7bit" {len(inner.as_bytes())} '
                f'NIL {_bodystructure(inner, lits)} 1 NIL NIL NIL NIL)')
    if part.is_multipart():
        kids = "".join(_bodystructure(p, lits) for p in part.get_payload())
        return f'({kids} {q(part.get_content_subtype())} ("boundary" "b") NIL NIL NIL)'
    params = [(k, v) for k, v in (part.get_params() or [])[1:]]
    disp = part.get_content_disposition()
    dparams = [(k, v) for k, v in (part.get_params(header="content-disposition") or [])[1:]]
    basic = (f"{q(part.get_content_maintype())} {q(part.get_content_subtype())} {plist(params)} "
             f"{q(part['Content-ID'])} NIL {q(part.get('Content-Transfer-Encoding', '7bit'))} "
             f"{len(str(part.get_payload()))}")
    if part.get_content_maintype() == "text":
        basic += " 1"
    disp_s = f"({q(disp)} {plist(dparams)})" if disp else "NIL"
    return f"({basic} NIL {disp_s} NIL NIL)"


class FakeIMAP:
    def __init__(self, server: FakeServer) -> None:
        self.s = server
        self.selected: Folder | None = None
        self.readonly = False
        self.literal: bytes | None = None
        self._untagged: dict[str, list] = {}

    # connection
    def login(self, user, password):
        if self.s.passwords and self.s.passwords.get(user) != password:
            raise imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Authentication failed.'")
        return "OK", [b"Logged in"]

    def logout(self):
        return "BYE", [b"bye"]

    def shutdown(self):
        pass

    def _log(self, *cmd):
        self.s.commands.append(cmd)

    # mailbox-level
    def list(self, directory='""', pattern="*"):
        self._log("LIST")
        out = []
        for f in self.s.folders.values():
            flags = "\\HasNoChildren" + (f" {f.special}" if f.special else "")
            out.append(f'({flags}) "." {f.name}'.encode())
        return "OK", out

    def select(self, mailbox="INBOX", readonly=False):
        name = _unquote(mailbox)
        self._log("EXAMINE" if readonly else "SELECT", name)
        if name not in self.s.folders:
            self.selected = None
            return "NO", [b"Mailbox doesn't exist"]
        self.selected, self.readonly = self.s.folders[name], readonly
        return "OK", [str(len(self.selected.messages)).encode()]

    def append(self, mailbox, flags, date_time, message):
        name = _unquote(mailbox)
        self._log("APPEND", name, flags)
        if name not in self.s.folders:
            return "NO", [b"[TRYCREATE] Mailbox doesn't exist"]
        f = set(flags.strip("()").split()) if flags else set()
        self.s.add(name, message, flags=f)
        return "OK", [b"[APPENDUID 1 1] Append completed."]

    def create(self, mailbox):
        self.s.add_folder(_unquote(mailbox))
        return "OK", [b"Create completed."]

    def delete(self, mailbox):
        name = _unquote(mailbox)
        if name not in self.s.folders:
            return "NO", [b"Mailbox doesn't exist"]
        del self.s.folders[name]
        return "OK", [b"Delete completed."]

    def rename(self, old, new):
        f = self.s.folders.pop(_unquote(old))
        f.name = _unquote(new)
        self.s.folders[f.name] = f
        return "OK", [b"Rename completed."]

    def subscribe(self, mailbox):
        return "OK", [b""]

    def unsubscribe(self, mailbox):
        return "OK", [b""]

    def expunge(self):
        self._log("EXPUNGE")
        f = self.selected
        f.messages = [m for m in f.messages if "\\Deleted" not in m.flags]
        return "OK", [None]

    def response(self, code):
        return code, self._untagged.pop(code, [None])

    # UID commands
    def uid(self, command, *args):
        command = command.upper()
        literal, self.literal = self.literal, None
        self._log("UID", command, *args, *([("literal", literal)] if literal is not None else []))
        return getattr(self, f"_uid_{command.lower()}")(list(args), literal)

    def _seq(self, m: Msg) -> int:
        return self.selected.messages.index(m) + 1

    def _parse_set(self, s: str) -> list[Msg]:
        want = {int(x) for x in s.split(",")}
        return [m for m in self.selected.messages if m.uid in want]

    def _uid_search(self, args, literal):
        toks = list(args)
        if toks and toks[0].upper() == "CHARSET":
            if not self.s.utf8_search:
                return "NO", [b"[BADCHARSET] Unsupported charset"]
            toks = toks[2:]

        def value():
            if toks:
                return _unquote(toks.pop(0))
            return literal.decode("utf-8")

        def expr() -> set[int]:
            key = toks.pop(0).upper()
            all_ = {m.uid for m in self.selected.messages}
            if key == "ALL":
                return all_
            if key == "UID":
                return {m.uid for m in self._parse_set(toks.pop(0))}
            if key == "OR":
                return expr() | expr()
            if key == "HEADER":
                name, v = toks.pop(0), value()
                return {m.uid for m in self.selected.messages
                        if v.lower() in (email.message_from_bytes(m.raw).get(name) or "").lower()}
            if key in ("FROM", "TO", "SUBJECT", "CC"):
                v = value().lower()
                return {m.uid for m in self.selected.messages
                        if v in _decoded_header(email.message_from_bytes(m.raw), key).lower()}
            if key in ("BODY", "TEXT"):
                v = value().lower()
                return {m.uid for m in self.selected.messages
                        if v in m.raw.decode("utf-8", "replace").lower()}
            raise imaplib.IMAP4.error(f"unsupported search key {key}")

        result: set[int] = set(m.uid for m in self.selected.messages)
        while toks or literal is not None:
            result &= expr()
            literal = None
            if not toks:
                break
        return "OK", [" ".join(str(u) for u in sorted(result)).encode()]

    def _uid_fetch(self, args, literal):
        uidset, items = args[0], args[1].upper()
        msgs = self._parse_set(uidset)
        if not msgs:
            return "OK", [None]
        out: list = []
        if "BODYSTRUCTURE" in items:
            for m in msgs:
                lits: list[bytes] = []
                text = f"{self._seq(m)} (UID {m.uid} BODYSTRUCTURE " \
                       f"{_bodystructure(email.message_from_bytes(m.raw), lits)})"
                pieces = text.split(_LIT)
                if len(pieces) == 1:
                    out.append(text.encode())
                    continue
                # imaplib shape: (text-before + {n}, literal) per literal, then the tail
                for before, lit in zip(pieces, lits):
                    out.append((f"{before}{{{len(lit)}}}".encode(), lit))
                out.append(pieces[-1].encode())
            return "OK", out
        for m in msgs:
            flags = "FLAGS (" + " ".join(sorted(m.flags)) + ")"
            seq = self._seq(m)
            body = None
            section = ""
            if "BODY.PEEK[]" in items:
                body, section = m.raw, "BODY[]"
            elif "HEADER.FIELDS" in items:
                fields = re.search(r"HEADER\.FIELDS \(([^)]*)\)", items).group(1).split()
                msg = email.message_from_bytes(m.raw)
                lines = [f"{k}: {v}" for k, v in msg.items() if k.upper() in fields]
                body = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
                section = f"BODY[HEADER.FIELDS ({' '.join(fields)})]"
            elif "BODY.PEEK[HEADER]" in items:
                body = _headers_text(m.raw).encode("utf-8") + b"\r\n\r\n"
                section = "BODY[HEADER]"
            if body is None:
                out.append(f"{seq} (UID {m.uid} {flags})".encode())
            elif self.s.flags_after_literal:
                out.append((f"{seq} (UID {m.uid} {section} {{{len(body)}}}".encode(), body))
                out.append(f" {flags})".encode())
            else:
                out.append((f"{seq} (UID {m.uid} {flags} {section} {{{len(body)}}}".encode(), body))
                out.append(b")")
        return "OK", out

    def _uid_store(self, args, literal):
        if self.readonly:
            return "NO", [b"Mailbox is read-only"]
        uidset, op, flags = args
        fl = set(flags.strip("()").split())
        for m in self._parse_set(uidset):
            if op.startswith("+"):
                m.flags |= fl
            else:
                m.flags -= fl
        return "OK", [None]

    def _copy_into(self, msgs: list[Msg], dest_name: str):
        dest = self.s.folders[dest_name]
        new = [self.s.add(dest_name, m.raw, flags=set(m.flags) - {"\\Deleted"}) for m in msgs]
        src = ",".join(str(m.uid) for m in msgs)
        self._untagged["COPYUID"] = [f"1 {src} {','.join(map(str, new))}".encode()]
        return dest

    def _uid_copy(self, args, literal):
        uidset, dest = args[0], _unquote(args[1])
        if dest not in self.s.folders:
            return "NO", [b"[TRYCREATE] Mailbox doesn't exist: " + dest.encode()]
        self._copy_into(self._parse_set(uidset), dest)
        return "OK", [None]

    def _uid_move(self, args, literal):
        if not self.s.supports_move:
            raise imaplib.IMAP4.error("UID command error: BAD [b'Unknown command MOVE']")
        uidset, dest = args[0], _unquote(args[1])
        if dest not in self.s.folders:
            return "NO", [b"[TRYCREATE] Mailbox doesn't exist: " + dest.encode()]
        msgs = self._parse_set(uidset)
        self._copy_into(msgs, dest)
        self.selected.messages = [m for m in self.selected.messages if m not in msgs]
        return "OK", [None]

    def _uid_expunge(self, args, literal):
        if not self.s.uidplus:
            raise imaplib.IMAP4.error("UID command error: BAD [b'Unknown command EXPUNGE']")
        want = {m.uid for m in self._parse_set(args[0])}
        self.selected.messages = [
            m for m in self.selected.messages
            if not (m.uid in want and "\\Deleted" in m.flags)
        ]
        return "OK", [None]
