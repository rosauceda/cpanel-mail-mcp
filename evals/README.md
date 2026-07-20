# cpanel-mail-mcp evaluations

Read-only questions an LLM should be able to answer using the tools this
server exposes. Each question exercises multiple tool calls and mirrors
tasks a real user would ask about their mailbox.

`questions.xml` follows the schema defined by the `mcp-builder` skill: one
`<qa_pair>` per question, each with `<question>` and `<answer>` tags. Answers
are single strings that can be verified by exact-match after basic
normalization (lowercase + strip).

## Running

Point your evaluation harness at a **populated test mailbox** — the answers
in this file assume a specific fixture of ~50 messages seeded with the
patterns described in `fixture-notes.md`. Adapt the questions/answers to
your own mailbox if you use different fixture data.

## Design notes

* No question mutates state (no `send`, `delete`, `move`, etc.).
* Every question needs at least two tool calls to answer (e.g. list → read
  a specific message, or search → count matching UIDs).
* Answers are stable — dates, counts, and header values in the fixture
  don't drift between runs.
