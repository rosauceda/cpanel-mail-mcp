# Fixture assumptions for `questions.xml`

The answers in `questions.xml` assume a mailbox seeded with the following
data. Use `admin add-user` to create a test account, then IMAP-APPEND these
messages (or bring your own mailbox and rewrite the questions).

Seeded messages in `INBOX`:

| UID | From                       | To                    | Subject                                         | Date        |
|-----|----------------------------|-----------------------|-------------------------------------------------|-------------|
| 1   | notifications@github.com   | you@example.com       | [rosauceda/cpanel-mail-mcp] Issue #42 opened     | 2026-07-01  |
| 2   | billing@stripe.com         | you@example.com       | Your July invoice from Acme                      | 2026-07-05  |
| 3   | juan@dominio.com           | you@example.com       | Reunión semanal — jueves 10am                    | 2026-07-08  |
| 4   | juan@dominio.com           | you@example.com       | Re: Reunión semanal — jueves 10am                | 2026-07-08  |
| 5   | maria@dominio.com          | you@example.com, juan@dominio.com | Presupuesto Q3 revisado                | 2026-07-09  |
| 6   | notifications@github.com   | you@example.com       | [rosauceda/cpanel-mail-mcp] Issue #43 opened     | 2026-07-10  |
| 7   | billing@stripe.com         | you@example.com       | Invoice paid — thank you                         | 2026-07-12  |
| 8   | jenkins@ci.dominio.com     | you@example.com       | Build #1234 failed on main                       | 2026-07-13  |
| 9   | juan@dominio.com           | you@example.com       | Contrato firmado (PDF adjunto)                   | 2026-07-14  |
| 10  | maria@dominio.com          | you@example.com       | Feliz cumpleaños! 🎉                             | 2026-07-15  |

* Message UID 9 has an attachment `contrato_v3.pdf` (mime `application/pdf`).
* Messages 3 and 4 form a thread (References/In-Reply-To linked).
* Folder `INBOX.Archivo` exists with 3 messages older than 2026-06-01.
* Folder `INBOX.Sent` has 7 outgoing messages.

If you seed a different fixture, update the answers accordingly.
