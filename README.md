# recall

Personal knowledge base on MongoDB Atlas. Feed it URLs, files, and notes;
search them by meaning. Step one of a three part build: knowledge base,
then metrics, then an agent with memory of both.

## Setup

### 1. Atlas cluster

1. Sign in at https://cloud.mongodb.com and create a project.
2. Build a cluster. Pick **M10** (smallest dedicated tier) so the startup
   credits get used; M0 works for a trial but is throttled and shared.
3. Under **Database Access** create a user with read/write on any database.
4. Under **Network Access** add your current IP (or 0.0.0.0/0 while testing).
5. **Connect -> Drivers** and copy the `mongodb+srv://` connection string.

### 2. Voyage AI key (embeddings)

Create it inside Atlas: project sidebar -> **AI Model APIs** -> **Model API
Keys** -> Create. Leave Cloud Provider and Geography on **Any (Agnostic)**.
Such keys bill through the Atlas project (and its credits), carry no
payment-method throttle, and work on the client's default endpoint with
every Voyage model. A US- or EU-scoped key only serves a subset of models
and needs `VOYAGE_BASE_URL` pointed at its regional host.

Keys from the standalone dashboard.voyageai.com also work, but without a
payment method they are limited to 3 requests and 10K tokens per minute.

### 3. Local

```bash
cp .env.example .env     # then fill in MONGODB_URI and VOYAGE_API_KEY
uv sync
uv run recall init       # creates the vector search index
```

The index builds asynchronously. `recall search` returns nothing until the
Atlas UI shows the index as Active, usually under a minute.

## Use

```bash
uv run recall add https://example.com/some-article --tag reading
uv run recall add ~/notes/niri.md --tag desktop --tag config
cat snippet.txt | uv run recall add - --title "that snippet"
uv run recall add paper.pdf
uv run recall add https://example.com/paper.pdf   # a PDF at a URL is read as a PDF

uv run recall search "how does the compositor reload its config"
uv run recall search "sleep tracking" --tag health -n 10 --full
uv run recall search "niri crash last week"          # time words become a date window
uv run recall search "what did I work on yesterday"  # only a time: that day's sessions, newest first
uv run recall search "descope" --since 2026-11-01 --until 2026-11-15

uv run recall catch-up --days 7    # the profile, then each recent session and what it left open
uv run recall list
uv run recall rm file:/home/you/notes/niri.md
```

Search returns the best chunk of each source, so one long note cannot fill
the page. Time words in a query ("yesterday", "last week", "3 days ago", "past 3
days", "over the weekend", "in October", "last year", "since 2026-11-20", "on
Monday") are read in `RECALL_TZ` (default:
the machine's zone) and filter both retrievers by `created_at`; the rest of
the query is ranked as usual. The search indexes need `created_at` as a
field; `recall init` adds it to older ones (they keep answering while they
rebuild).

Install it on your PATH with `uv tool install --editable .` and drop the
`uv run` prefix. Outside the project folder, `recall` looks for its env file
at `$RECALL_ENV`, then `./.env`, then `~/.config/recall/env`, then the
project's own `.env`, so copy `.env` to `~/.config/recall/env` once.

## Folder sync

`recall add` saves a file once; when the file changes, recall keeps answering
from the old copy. `recall sync` keeps the files and folders listed in
`~/.config/recall/sync.toml` current: it saves what is new or changed and
removes what is gone. Each chunk carries its file's hash, so a pass only
touches what changed.

```bash
cp contrib/sync.example.toml ~/.config/recall/sync.toml   # then edit the paths
recall sync --dry-run      # what would be saved, removed, skipped, and why
recall sync                # do it once
```

```toml
[[watch]]
path = "~/.claude/projects/-home-you/memory"   # a folder or one file
title = "Claude memory"       # folder: label before each file name
tags = ["claude", "memory-note"]
include = ["*.md"]            # default *.md and *.txt
exclude = ["MEMORY.md", "drafts/"]
```

Everything in recall can be read by the models that search it, so sync refuses
some files whatever the config says: key and env files, binary files, and any
file whose text looks like it holds a secret (an API key, a private key, a
password in a URL, an SSN or EIN). They show up as `skip` lines with the kind
of secret and the line number, never the value. Inside a watched folder,
hidden files and folders and symlinks are skipped too; give a dotfile its own
`[[watch]]` entry to save it. A copy saved before a secret appeared in the
file is removed.

A file saved earlier with `recall add` keeps its title and tags unless the
watch sets its own. A source sync did not save is never removed by it. When a
watch path is missing (an unmounted disk), nothing is removed. When a
`[[watch]]` entry leaves the config, its files stay until `recall sync --prune`.

To keep it running, install the service. It checks file names, sizes and
change times every minute and only opens files and the database when one
moved; once an hour it compares with the database anyway.

```bash
cp -r contrib/systemd/recall-sync.service{,.d} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now recall-sync.service
journalctl --user -u recall-sync -f     # one line per pass that changed something
```

## Session notes

A Claude Code session leaves a transcript on disk and nothing else: what was
decided, and why, is hard to find a week later. `recall sessions` reads
transcripts that have been quiet for 30 minutes, asks Claude for a short
record, and stores it as `session:<id>` with the tag `session-note`:

```
Session: Search reranker tuned, sync loop fixed (2026-11-09)
## Done        what was built, changed, fixed or found out
## Decisions   each one with its reason
## Still open  unfinished, deferred, waiting on someone
## Where       paths, services, commands, commit ids, dates
```

```bash
recall sessions --dry-run              # what is due, and the estimated cost
recall sessions                        # summarize sessions that went quiet since the first run
recall sessions --since 30d --dry-run  # reach back: count and cost first
recall sessions --since 30d            # then do it
recall search "why did we change the reranker" --tag session-note
```

- It reads the `projects` folder of `~/.claude` and of every other folder in the home
  directory whose name starts with `.claude` (a second account, a second desktop app), so
  a new one is picked up without a setting. `RECALL_SESSION_ROOTS`, colon-separated, names
  the folders instead. Sub-agent transcripts are left out.
- Only what was said goes to the model: your messages, the assistant's replies,
  and one line per command run or file edited. Tool output, images and
  reasoning are left out, and a message over 6,000 characters keeps its start
  and end.
- "Quiet for 30 minutes" is the trigger, not the session ending: a desktop
  session can stay open for days. When a summarized session continues, only
  the new part is read and the note is rewritten from the old note plus the
  new part.
- Sessions with nothing worth finding later get no note. A note that looks
  like it contains a secret is not stored (`secret` in the output).
- The model is `RECALL_SUMMARY_MODEL`, else `RECALL_MODEL`, else
  `claude-opus-5-5`, at effort `RECALL_SUMMARY_EFFORT` (default `low`). A
  session costs about two to eight cents on Opus 5.5 (three test sessions:
  34K tokens in, 2.8K out, $0.19); Sonnet 5.5 is half that and Haiku 5.5
  about a fortieth. `--dry-run` gives the estimate for what is due.
- State (how far each transcript was read, and its note) is
  `~/.local/state/recall/sessions.json`. Only sessions that change after the
  first run are picked up, so installing the service never starts a backfill
  by itself.

```bash
cp -r contrib/systemd/recall-sessions.service{,.d} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now recall-sessions.service
```

## Open items

Every session note ends with a "Still open" list, and `recall catch-up`, the `open_items`
request and anything built on them show those lists. A note is a record of its day and is
never rewritten, so an item stayed on its list after a later session did it.

`recall-sessions.service` now settles them. After each pass it shows every new session note
(Claude Code and Hermes) to the model next to the items earlier sessions left open, and asks
which of them this session settled:

- **done**: the note shows the thing was done.
- **dropped**: it was decided against, or what it concerns was removed or replaced.
- **carried**: the note lists the same thing again as still open, so the older copy goes and
  the newer one stands.

```bash
recall open                        # what is open now, by session, each item with its id
recall open --closed               # the latest closures: how, why, the words quoted, the session that did it
recall open --done open:1a2b3c4d5e6f --why "did it by hand"
recall open --reopen open:1a2b3c4d5e6f
recall open --settle --since 14d --dry-run   # how many notes would be asked about, and the cost
recall open --settle --since 14d             # reach back once; the service only looks at the last 3 days
```

What the model may decide, and what code decides:

- The model sees one note and at most 40 open items: those of earlier notes (up to 60 days
  older) from the same folder, and those of notes close to it in meaning.
- Code accepts a closure only for an item that was shown, with a reason, and with words
  quoted from the note that are really in the note. A second call then looks at each proposed
  closure alone and must agree; "work on the same subject" and "partly done" are a no.
- At most eight closures per note. A note is looked at once, and again if its session
  continues and the note is rewritten.
- The notes are never changed, so a search still finds what was open on that day. Closures
  live apart in the `open_items` collection; `--reopen` deletes one and the item is back.

An item is open while its note still lists it and no closure exists for it. That only takes
off what a later note shows was settled. On the first run over 14 days of notes (97 notes,
294 items) it closed 31 items, 21 of them from that window; most of what stayed is a step
only the owner can take or a caveat no session came back to, and those leave the list with
`--done` or when they age out of the window. One note costs
about 6,000 tokens in (a cent or two on Sonnet 5.5, less on Haiku), with the same model
setting as `recall learn` (`RECALL_LEARN_MODEL`, else `RECALL_SUMMARY_MODEL`).

## Health check

`recall doctor` checks every part and prints one line each: the services are
running and not restarting in a loop, Atlas answers, both search indexes are
ready, the plain indexes exist (a warning when one is missing), metrics are arriving, Voyage answers, no Hermes events are stuck in the
spool, the vault key is present and private, and the Claude API key works.
With `RECALL_ONLINE_HEALTHZ=https://name.example/healthz` set, it also checks
that the hosted recall (see "Online") is up and can reach its database.
It exits 1 when something failed.

```bash
recall doctor
recall doctor --test-alert     # one test message to the phone
```

`recall-doctor.service` runs it every ten minutes with `--alert`. A failure
seen on two checks in a row is sent to the phone through Bark (`BARK_KEY`;
encrypted when `BARK_ENC_KEY` holds the 32-character key set in the Bark app),
or as a desktop notification without the key. A new problem is sent as a
critical alert, which rings even when the phone is silenced. One more message says when everything is fine again, and a
reminder goes out once a day while something stays broken. Warnings (no
events for 36 hours, the Claude API unreachable) are shown but never sent.
It cannot notice the machine being off, or Proton Pass being unreachable, since
the doctor cannot start then either.

```bash
cp -r contrib/systemd/recall-doctor.service{,.d} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now recall-doctor.service
```

## Search quality

`recall eval` answers "did that change make search better or worse?". It reads a question
set, puts each question through the same search the assistants use, and reports how often
the answer came first, how often it was among the results, and which questions missed.

```bash
cp contrib/eval.example.toml ~/.config/recall/eval.toml   # then write your own questions
recall eval                 # the numbers, and every question that went wrong
recall eval --save          # keep this run as the baseline
recall eval --no-rerank     # the same questions with one thing changed, against the baseline
recall eval -v              # every question with its place and top score
```

```toml
[[case]]
q = "where do the services read their secrets from"
expect = "file:~/notes/secrets-proton-pass.md"     # or a list: any of them counts
avoid = "file:~/notes/secrets-1password.md"        # a replaced note that must not come first

[[case]]
q = "best sourdough hydration ratio"
expect = []                                        # nothing stored answers it: stay quiet
```

- `expect` and `avoid` are source ids as `recall list` prints them; `*` matches any run of
  characters and `file:~/` is the home folder.
- A question with an empty `expect` passes when the top score stays under 0.55, the mark
  below which the Hermes provider hands nothing over.
- A question whose expected source is no longer stored is listed apart and not counted:
  search cannot find what is not there.
- With a baseline saved (`~/.local/state/recall/eval.json`), every later run ends with what
  moved: which questions got better and which got worse.
- Each question costs one embedding call and one rerank call. Nothing is written to the
  database.

Write the questions from the sources, the way they would really be asked, and not from what
search returns today; otherwise the set only measures what already works. The question set
is personal, so it stays outside the repository.

## Hermes memory provider

Hermes can use recall as its external memory, so that what it knows comes from
the same store every other assistant reads and writes: saved notes, memories,
Claude session notes, synced files, and its own earlier exchanges with you
(stored as events by the `recall-events` hook).

Three parts:

- `recall serve` (`recall-serve.service`) holds the database and Voyage
  connections and answers on a Unix socket only this user can open
  (`$XDG_RUNTIME_DIR/recall.sock`). A request is one JSON line:
  `{"op": "context", "query": "..."}`. It runs the normal hybrid search with a
  smaller rerank pool (12 candidates instead of 30) and returns only what the
  reranker scores at 0.55 or higher, as short excerpts. On a test set,
  questions about saved material scored 0.6 to 0.9 at the top and unrelated
  prompts 0.3 to 0.45, so small talk gets nothing. A warm request takes about
  0.3 to 0.5 seconds.
- `contrib/hermes-memory/recall/` is the Hermes plugin. It needs only the
  socket: no keys, no Python packages. It adds no tools (the MCP server
  provides them). If recall is down or slow, the turn goes on without it. It
  asks for events too, so earlier Hermes exchanges can come back, with two
  things kept out: desktop notifications (excluded inside the search, so they
  take no place among the candidates) and the exchanges of the conversation
  that is asking, which Hermes already has in front of it.
- `recall learn` (`recall-learn.service`) is what turns stored chats into
  memory. See the next section.

Within one conversation the plugin behaves like memory, not like a search
box: an item it handed over is not handed over again on the next turn (the
conversation still has it), and a short follow-up such as "and the other
one?" is searched together with the message before it.

```bash
cp -r contrib/systemd/recall-serve.service{,.d} ~/.config/systemd/user/
cp -r contrib/systemd/recall-learn.service{,.d} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now recall-serve.service recall-learn.service

cp -r contrib/hermes-memory/recall ~/.hermes/plugins/
# ~/.hermes/config.yaml:  memory: provider: 'recall'   then restart Hermes
```

Hermes takes one external provider at a time, so this replaces whichever one
`memory.provider` named; putting the old name back undoes it. Settings and
their defaults are in `contrib/hermes-memory/recall/README.md`.
`hermes memory status` shows what recall has learned and when it last read a
conversation.

## Learning from Hermes conversations

Storing every exchange is not memory. `recall learn` reads each Hermes
conversation once it has been quiet for 20 minutes and does three things:

- **Facts.** Lasting facts about you, your setup and your decisions become
  memories (tags `memory`, `learned`, `hermes`), the same kind the `remember`
  tool stores, so every assistant finds them.
- **Corrections.** When a new fact replaces an older memory, the older one is
  retired: moved out of `chunks` into the `retired` collection, so no search
  on any instance finds it, and nothing is lost.
- **A record.** A conversation with substance gets a note like a Claude Code
  session does (`session:hermes:<id>`, tags `session-note`, `hermes`), which
  a normal search finds. Hermes exchanges themselves are events, and events
  are left out of a normal search.

```bash
recall learn --dry-run --since 30d   # what would be read, and the estimated cost
recall learn --since 30d             # read the older conversations once
recall learned                       # facts learned so far
recall learned --retired             # memories a newer fact replaced
recall learned --restore memory:0123456789ab
recall learned --profile             # the profile Hermes is given
```

It also keeps a **profile**: about 300 words on who you are, how you like to
be worked with, what you use, your projects and what you did lately, written
from the `user-*` and `feedback-*` notes your assistants keep
(`RECALL_PROFILE_NOTES`, a regex on titles), the head of each `project-*` note
(`RECALL_PROFILE_PROJECTS`), the memories, and the titles of the last two
weeks' session notes. It is rewritten when those change, at most every six
hours. The Hermes plugin puts it in the system prompt, and every MCP client
reads it with the `profile` and `catch_up` tools, so what one assistant knows
about you the others have from the start.

Where the conversations come from: the gateway hook that feeds the event
stream cuts each side of an exchange to 500 characters. That is enough to
search and too little to learn from, so the plugin also hands each finished
exchange, whole, to `recall serve` (`{"op": "turn", ...}`), which keeps it in
the `turns` collection for 120 days. Exchanges that exist only as events
(older ones, and the cloud agent's) are copied in as they are. `turns` is never
searched, and an exchange that looks like it holds a secret is not kept.

**Your name in what is stored.** Set `RECALL_OWNER=Ada` in the env file and the
prompts that write stored text name you: learned facts read "Ada runs niri on
CachyOS", session notes and the profile are about Ada, and the example the
`remember` tool shows assistants uses the name too. Without it they say "the
user". Only stored text changes; the instructions that tell an assistant how to
use recall always say "the user", since that assistant is talking to you. The
name is read when a command or service starts, so restart the services after
setting it. Memories already stored are not rewritten.

What the model may decide, and what code decides:

- The model (`RECALL_LEARN_MODEL`, else `RECALL_SUMMARY_MODEL`) proposes facts
  from what you said or what Hermes reports having done at your request, not
  from what was looked up. A second call then checks each proposed fact
  against what recall holds on exactly that fact, and drops what is already
  known.
- Code stores a fact only if it is one sentence of 20 to 400 characters with
  no secret in it and no stored memory already says it (vector similarity
  0.94 or higher). At most six facts are kept and three memories retired per
  conversation read.
- A memory can only be replaced or retired if it was shown to the model in
  that call. Files synced from disk are never changed: when one is out of
  date, the new fact is stored next to it and ranks above it by recency.
- A wrong learned fact is removed like any memory (`forget`, or
  `recall rm memory:...`).

## A private instance

A second recall can know everything the main one knows while nothing it
learns flows back. It has its own database on the same cluster, its own env
file, socket and MCP launcher, and is meant for one Hermes profile.

- `RECALL_DB=private` and `RECALL_READ_DBS=recall`: every write (remember,
  notes, stored exchanges) goes to `private`; search also reads the databases
  in `RECALL_READ_DBS` and never changes them. Only search reads across; lists
  and recent events show the instance's own database.
- `MONGODB_USERNAME` and `MONGODB_PASSWORD` replace any login in
  `MONGODB_URI`, so the env file can hold the bare cluster address and a
  database user of its own. Give that user `readWrite` on `private` and `read`
  on `recall`, and keep the main user off `private`: then Atlas enforces the
  split, not only this code.
- `RECALL_SERVE_STORE=1`: `recall serve` accepts `{"op": "store", ...}` and
  saves a finished exchange in its own database directly. The private instance
  has no Kafka keys, so nothing it stores passes through the event stream (whose
  sink writes to the main database). The Hermes plugin sends exchanges this way
  when the profile's `recall.json` has `"store_turns": true`.

```bash
cp contrib/systemd/recall-serve-private.service ~/.config/systemd/user/
cp contrib/bin/recall-mcp-private ~/.local/bin/
# ~/.config/recall/private.env: RECALL_ENV (its own path), MONGODB_URI, MONGODB_USERNAME,
# MONGODB_PASSWORD, VOYAGE_API_KEY, VOYAGE_MODEL, RECALL_DB, RECALL_READ_DBS, RECALL_SERVE_STORE
recall-pass-run --env-file ~/.config/recall/private.env -- recall init
systemctl --user daemon-reload && systemctl --user enable --now recall-serve-private.service
# In the Hermes profile: recall.json {"socket": "$XDG_RUNTIME_DIR/recall-private.sock", "store_turns": true},
# and its recall MCP server command set to ~/.local/bin/recall-mcp-private.
```

Point nothing else at the private socket or launcher. Check a new setup with a
marker: say a made-up word in the private profile, then look for it in the main
database and in `~/.local/state/recall/spool.jsonl`. Anything that notifies a
phone from that profile can come back as a desktop notification and be stored
by the main instance, so turn those off for the profile.

## How it works

- `sources.py` turns a URL (readability extraction), file, PDF, or stdin
  into text.
- `chunk.py` splits text into ~1500 character paragraph-aware chunks with
  overlap.
- `embed.py` embeds chunks with Voyage (`voyage-4`, 1024 dims). Queries
  use `input_type="query"`, documents use `"document"`; Voyage trains for
  that asymmetry.
- `ingest.py` stores one document per chunk in the `chunks` collection and
  replaces all chunks of a source on re-add.
- `sync.py` compares the files in `sync.toml` with what is stored and saves
  or removes the difference (`recall sync`).
- `sessions.py` turns quiet Claude Code transcripts into session notes;
  `learn.py` turns quiet Hermes conversations into facts, a note and the
  profile; `doctor.py` runs the health checks and decides when to alert;
  `serve.py` answers context requests on a local socket.
- `search.py` retrieves 30+ candidates with `$rankFusion` over Atlas Vector
  Search (meaning) and Atlas Search full text on `title` and `text` (exact
  words: names, ids, error strings), reranks them with Voyage `rerank-3`, and
  adds a small recency bonus (`RECALL_RECENCY_WEIGHT`, default 0.05, halving
  every `RECALL_RECENCY_HALF_LIFE_DAYS`, default 30) so the newer of two
  equally relevant chunks wins. A session note is a record of one day and is never
  brought up to date, so when a newer source of another kind (a memory, a synced
  note, a saved page) scores within `RECALL_RECORD_MARGIN` of it (default 0.075;
  0 turns this off), the session note is placed just below that source. With
  `RECALL_OWNER` set, the reranker gets the query and each candidate with "the user"
  written as that name: notes kept by assistants say "the user's watch" where a
  question says "Ada's watch", and the reranker otherwise reads them as two people.
  `recall init` creates both indexes, and the
  plain index on tag and date that the lists and counts (memories, events,
  session notes) read from; run it again after an update that adds one. Without
  the text index it falls back to vector search alone and says so on stderr;
  `RECALL_RERANK_MODEL=` (empty) turns reranking off. Scores shown are rerank
  relevance (0 to 1) plus the bonus.

Document shape:

```json
{
  "source": "https://... | file:/abs/path | stdin:<hash>",
  "title": "...",
  "chunk_index": 0,
  "text": "...",
  "embedding": [1024 floats],
  "tags": ["reading"],
  "model": "voyage-4",
  "created_at": ISODate
}
```

## Tests

```bash
uv run pytest -q                           # recall itself
node --test deploy/cloudflare/test/*.js    # the Worker's sign-in and the app's files
```

Neither needs a database, a key or the network. `.github/workflows/tests.yml` runs both
on every pull request and on main.

## Do not change later without re-embedding

`VOYAGE_MODEL` and `VOYAGE_DIMENSIONS`. Vectors from different models are
not comparable, and the index is built for a fixed dimension count. If you
change them, drop the collection, recreate the index, and re-add everything.

## Vault

`recall vault` is for things you want to find fast but never want in plain
form in a cloud database: ID numbers, a lease, insurance policies, the wifi
password. Entries are encrypted on this machine before upload.

```bash
recall vault add document.pdf --title "Some document" --note "renews 2030" --tag docs
printf 'SSID: x\npassword: y\n' | recall vault add - --title "Home wifi"
recall vault get "some document"       # fuzzy match on title and note
recall vault get "home wifi" --copy    # to clipboard instead of the terminal
recall vault get "lease" --out ~/Downloads/lease.pdf
recall vault list
recall vault rm vault:xxxxxxxxxxxx
```

What leaves the machine: the title, the note, and the ciphertext. Only the
title and note are embedded for search, so document contents are never sent
to the embedding API unless you pass `--embed-content`. Regular `recall
search`, `recall list`, and every agent tool exclude the `vault` tag, so the
assistant cannot see or retrieve vault entries; it will tell you to use
`recall vault get`.

The key is `~/.config/recall/vault.key` (mode 600, created on first use).
**Back it up.** Without it the entries cannot be decrypted by anyone,
including you. Entries are capped at 12 MB each.

`recall help` prints a one-screen cheat sheet of all of the above.

## Metrics

`recall metrics` stores system samples and manual entries in a time series
collection named `metrics` (time field `ts`, meta `{host, kind, name?}`).

```bash
recall metrics init                       # create the time series collection
recall metrics collect                    # one system sample from this machine
recall metrics collect --loop -i 60       # keep sampling every 60 s
recall metrics add sleep_hours 7.5        # manual entry, now
recall metrics add weight_kg 72 --at 2026-10-23T08:00 --note "after run"
recall metrics show --since 24h --bucket hour
recall metrics show --name sleep_hours --since 30d
```

System samples carry `cpu_pct`, `load1/5/15`, `mem_pct`, `mem_used_gb`,
`swap_pct`, `disk_pct.<mount>` (one entry per block device), `cpu_temp`,
`net_rx_gb`, `net_tx_gb`, `procs`, and `uptime_h`. Manual entries carry
`value` and an optional `note`, with the metric name in `meta.name`.

### Collector as a systemd user service

`contrib/systemd/recall-metrics.service` runs `recall metrics collect --loop`
(one sample a minute) as a long-lived service. It is not a one-minute timer
because the env file holds Proton Pass references, and resolving them 1,440
times a day would mean a Proton round trip every minute. Each unit in
`contrib/systemd/` has a `pass-run.conf` drop-in that starts it under
`contrib/bin/recall-pass-run` (`pass-cli run`) with recall's TPM-encrypted
Proton Pass access token.

Secrets setup (Proton Pass):
`~/.config/recall/env` holds `pass://vault/<NAME>/credential` references. Create a
Proton Pass personal access token with read access to that vault, then store it
TPM-encrypted as the systemd credential `recall-pass-token`:

```bash
cp contrib/bin/recall-pass-run contrib/bin/recall-pass-token-store ~/.local/bin/
recall-pass-token-store   # paste the token; it is never echoed or written in plain text
```

Tokens expire (one year at most). After `pass-cli personal-access-token renew`, which
issues a new token string, run `recall-pass-token-store` again. The wrapper keeps its
own `pass-cli` session in `~/.local/state/recall/pass-session` and logs in again only
when the 2-hour token session has ended.

```bash
mkdir -p ~/.config/systemd/user
cp -r contrib/systemd/recall-metrics.service{,.d} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now recall-metrics.service
```

The service reads `~/.config/recall/env`, so that file must exist.

### Dashboard

Atlas Charts is activated for the project. The dashboard **recall metrics**
has three continuous-line charts against `recall.metrics`: CPU and memory
percent, CPU temperature, and load average. Add more from the Visualization
section of the Atlas project by dragging `ts` to the X axis and any numeric
field to the Y axis.

## Agent

`recall chat` is Claude with the knowledge base and metrics as tools, plus
a memory it writes to itself. `recall ask "..."` is the one-shot form.

```bash
recall ask "what did I save about niri config reloading?"
recall chat
```

Needs `ANTHROPIC_API_KEY` in the env file. Model and effort come from
`RECALL_MODEL` (default `claude-opus-5-5`) and `RECALL_EFFORT` (default
`medium`).

Tools the model can call: `search_knowledge`, `remember`, `forget`,
`list_memories`, `list_sources`, `save_source`, `metrics_summary`,
`metrics_manual`, `record_metric`.

How memory works:

- A memory is a single chunk in the same `chunks` collection, tagged
  `memory` with source `memory:<id>`, so `search_knowledge` (and
  `recall search`) finds it alongside saved articles.
- The system prompt tells the model to search before answering personal
  questions and to call `remember` when you state something durable.
- When a chat ends, the agent gets one extra turn to store anything worth
  keeping, then the whole transcript is saved to the `sessions` collection
  with a one-line summary. `--no-save` skips both.

Each search or remember is one Voyage embedding call. With an Atlas-issued
key that is not a concern; a standalone Voyage key without a payment
method is limited to 3 calls a minute and makes a chatty session stall.

Implementation notes (`src/recall/agent/`): a manual loop over
`client.beta.messages.stream` so text streams while tools run; the system
prompt carries a cache breakpoint and the tool list has a fixed order so the
prefix caches; the current time and host are prepended to each user turn
rather than placed in the system prompt, which would defeat the cache;
`fallbacks="default"` is enabled so a safety-classifier decline is retried
on a fallback model inside the same call; assistant content, including
thinking blocks, is passed back unchanged.

## MCP server

`recall mcp` serves recall over MCP (stdio; for HTTP see [Online](#online)), so Claude Code, Codex, Kiro,
Copilot, Cursor and Hermes can search and save directly. Each client starts
its own copy. Tools: `profile`, `catch_up`, `search`, `remember`, `forget`, `list_memories`,
`list_sources`, `save_url_or_file`, `save_note`, `recent_events`,
`metrics_summary`, `metrics_manual`, `record_metric`. They share their code
with the `recall chat` agent. The vault is not reachable: search excludes it
and no tool touches it.

`remember` and `save_note` refuse text that looks like it holds a secret (the same
patterns folder sync uses), here, online and in the app, and say which kind and on
which line; such things belong in the vault. `recall add` does not check: a page or
file you add yourself often shows an example key or connection string.

Results are kept small for the calling model's context: `search` returns 5
hits as 700-character excerpts around the matching words (`full=true` for
whole chunks), and `recent_events` folds notifications that share a title
into one line with a count, the time span, and the latest detail.

`profile` returns the standing profile; `catch_up` returns it followed by
every Claude Code and Hermes session note of the last few days with what each
left open, so a new session in any client can pick up where another stopped.
The server's instructions ask clients to call `profile` once when they do not
already know him.

Because `~/.config/recall/env` holds Proton Pass references, clients launch
`contrib/bin/recall-mcp`, which starts `recall mcp` under `recall-pass-run`
with the TPM-encrypted Proton Pass token (the same way the services start).
Startup takes about 2 seconds.

```bash
cp contrib/bin/recall-mcp ~/.local/bin/
claude mcp add --scope user recall -- ~/.local/bin/recall-mcp
codex mcp add recall -- ~/.local/bin/recall-mcp
```

Other clients take the same command (`~/.local/bin/recall-mcp`, no
arguments) in their MCP settings.

## Online

`recall mcp-online` serves the same tools over HTTP, so apps that are not on this
machine (claude.ai, a phone, another computer) can use recall after a sign-in. It
listens on `127.0.0.1:8791`; a tunnel publishes it at `RECALL_ONLINE_URL`.

recall is only the resource server. An authorization server (Descope) signs you in,
registers the apps and issues access tokens; recall issues nothing and stores no
client secrets. For every request it checks the token's signature against the
authorization server's published keys, the issuer, the audience (the public
address), the expiry, and that the signed-in user is listed in
`RECALL_ONLINE_SUBJECTS`. A valid token for any other user is refused. Each token
must also carry the three permissions `recall:read`, `recall:write` and
`recall:forget`; every tool checks its own again.

`save_url_or_file` is not offered online: it makes this machine fetch any address
or read any file path it is given. The vault tools are online only, behind the
owner's sign-in.

An agent that cannot sign in through a browser (a Hermes in the cloud)
gets an agent key instead: a random string starting `rk_` that the agent makes
and keeps itself. The server knows only its SHA-256, in
`RECALL_AGENT_KEYS=name:<hex>[,name2:<hex>]`. An agent key may search and save but
never forget or open the vault; those tools refuse it. The same check guards
`POST /serve`, which takes `recall serve`'s requests (`ping`, `context`, `profile`,
`status`, `turn`; not `store`) so a Hermes memory provider can work over HTTPS:

```bash
# on the agent's machine, keep the key; send only the hash
python3 -c "import secrets;print('RECALL_AGENT_KEY=rk_'+secrets.token_urlsafe(40))" >> .env
# Hermes there: recall.json {"url": "https://name.example/serve"}, and an MCP server
#   url: https://name.example/mcp   headers: {Authorization: "Bearer ${RECALL_AGENT_KEY}"}
```

```bash
# ~/.config/recall/online.env (no secrets):
#   RECALL_ONLINE_URL=https://name.example/mcp
#   RECALL_ONLINE_ISSUER=https://api.descope.com/v1/apps/<project id>
#   RECALL_ONLINE_SUBJECTS=<your Descope user id>
cp contrib/systemd/recall-online.service ~/.config/systemd/user/
cp -r contrib/systemd/recall-online.service.d ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now recall-online.service
curl -i -X POST http://127.0.0.1:8791/mcp     # 401 with a pointer to the sign-in
```

At the authorization server: an MCP server whose URL is exactly
`RECALL_ONLINE_URL` (it becomes the token's audience), the three scopes above,
and client registration (CIMD, DCR) switched on so apps can register themselves.
Keep sign-up closed; the allow-list is the second lock, not the only one.

### Web API (for the recall app)

The same server answers a small JSON API under `/api/v1` (`src/recall/api.py`), which
the recall app uses. It follows the rules above: every request carries an access
token, checked exactly as for MCP, and each route needs its permission.

| Route | Permission | What it returns or does |
|---|---|---|
| `GET /me` | any | who is signed in |
| `GET /status` | read | counts: memories, learned, retired, exchanges waiting |
| `GET /search?q=&limit=&tags=&events=1&full=1` | read | hits with kind, score, date and an excerpt |
| `GET /memories?limit=&learned=1` | read | recent memories |
| `POST /memories` `{fact, tags}` | write | stores one fact |
| `DELETE /memories/{id}` | forget | deletes one memory |
| `GET /retired`, `POST /retired/{id}/restore` | read, write | memories a newer fact replaced; put one back |
| `POST /notes` `{text, title, tags}` | write | saves a note |
| `GET /sources`, `GET /source?id=` | read | what is saved; one source's text |
| `GET /events?since=24h&source=` | read | notifications, digests and Hermes chats, newest first |
| `GET /events/counts?since=24h` | read | how many events arrived, by kind |
| `GET /profile` | read | the standing profile |

Vault entries are never returned. The app's pages do not hold a token: the Cloudflare
Worker signs the user in and adds the token to each request (see
`deploy/cloudflare/README.md`).

### The recall app

A small web app for a phone or a desktop, served from the same address
(`deploy/cloudflare/app`, plain HTML, CSS and JavaScript, no build step). Home has a
save box and tiles for memories, sources, what was learned lately, the last day's
events and what is waiting to be read; the other pages search, list memories (forget,
restore), show the timeline, the sources and one saved item. It can be added to the
home screen. `node deploy/cloudflare/preview.mjs` opens it on this machine with sample
data and no sign-in.

## Event stream (Confluent Kafka)

Events flow into the `events_raw` topic on the Confluent Cloud cluster
`recall-events` (Avro, schemas registered by Flink), and `recall stream sink`
embeds them and upserts them into `chunks` tagged `event`, so `recall search`
finds them. Secrets come from `~/.config/recall/env` (Proton Pass `pass://`
references), so every service runs under `recall-pass-run --env-file`.

| Command | Service | What it does |
|---|---|---|
| `recall stream notify` | `recall-notify` | desktop notifications to Kafka (one-time codes dropped) |
| `recall stream spool` | `recall-spool` | lines that local programs append to `~/.local/state/recall/spool.jsonl` to Kafka |
| `recall stream sink` | `recall-sink` | Kafka (events and hourly digests) to Atlas with embeddings |
| `recall stream send TEXT` | | one test event |

A Flink statement (`contrib/flink/events_digest.sql`) also writes one row per hour
and source to `events_digest`: how many events there were, from which apps, and the
start of each. The sink stores each row as one event with the tags `event` and
`digest` (source `event:digest:<source>:<hour>`), so "what happened this morning"
finds a few summaries instead of hundreds of single notifications.

Events stay out of plain `recall search` and the agent's `search_knowledge`
so they don't bury saved material. Use `recall search --events`, or any
`--tag` (`--tag hermes`); the agent passes `include_events` for "what happened
recently" questions.

### Notification noise

`recall stream notify` and `recall stream sink` drop notifications that match a
drop rule, and repeats of the same app and text within 7 days. Tether replays
the phone's history on every reconnect, so before this filter two thirds of
stored notifications were copies. Built-in rules (`src/recall/stream/noise.py`)
cover screenshots, saved logins, app updates, test popups, keyring prompts,
and "% off" promos. Add your own in `~/.config/recall/noise.toml`; the example
`contrib/noise.example.toml` drops phone feeds and promos and keeps money and
health alerts off the cloud.

```bash
cp contrib/noise.example.toml ~/.config/recall/noise.toml
systemctl --user restart recall-notify recall-sink
recall stream purge                       # dry run: what would be dropped, by reason
recall stream purge --yes                 # delete it
recall stream purge --yes --retention-days 90   # and expire notifications after 90 days
```

### Hermes

`contrib/hermes-hook/recall-events/` is a Hermes gateway hook on `agent:end`.
It holds no secrets: it appends each exchange (message and reply, 500 chars
each) to the spool, and `recall-spool` produces it.

```sh
cp -r contrib/hermes-hook/recall-events ~/.hermes/hooks/
hermes gateway restart
```

Install `contrib/systemd/recall-spool.service` with the same `pass-run.conf`
drop-in as `recall-sink`.

a cloud-hosted Hermes can't reach the desktop's spool and has no
Kafka client, so `contrib/hermes-hook/cloud-recall-events/` posts plain JSON to
the `events_inbox` topic with the Confluent Cloud REST API (which can't produce
Avro). Its API key belongs to a service account that may only WRITE
`events_inbox`. `recall stream sink` reads `events_inbox` as JSON next to the
Avro `events_raw`, with the same checks as the spool. Settings go in the cloud agent's
`.env`: `RECALL_EVENTS_URL`
(`https://<cluster>.<region>.aws.confluent.cloud/kafka/v3/clusters/<cluster-id>/topics/events_inbox/records`),
`RECALL_EVENTS_KEY`, `RECALL_EVENTS_SECRET`.
