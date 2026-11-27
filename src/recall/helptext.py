"""`recall help`: a cheat sheet, as opposed to `--help` which lists flags."""

TEXT = """\
recall: your personal knowledge base, metrics, and assistant.

SAVE THINGS
  recall add <url|file|->  [--tag t] [--title x]   save an article, file, PDF, or stdin
  recall list                                      what is stored
  recall rm <source>                               remove one source (id from `list`)
  recall sync [--dry-run] [--prune]                save new and changed files from sync.toml,
                                                   remove deleted ones (recall-sync.service loops it)
  recall sessions [--since 30d] [--dry-run]        a short note per finished Claude Code session:
                                                   done, decided, still open (recall-sessions.service)
  recall open [--closed] [--done ID] [--reopen ID] what sessions left open and no later session settled;
                                                   --closed lists what was taken off and why
  recall learn [--since 30d] [--dry-run]           read quiet Hermes conversations: keep lasting facts,
                                                   retire the ones they replace, write a record, keep
                                                   the profile current (recall-learn.service)
  recall learned [--retired] [--profile]           what was learned, what was retired, the profile;
                                                   --restore <id> brings a retired memory back

FIND THINGS
  recall search "..." [-n 8] [--tag t] [--full] [--events]
                                                   semantic search, no model call
  recall ask "..."                                 one question to the assistant
  recall chat                                      conversation; it remembers what you tell it
  recall mcp                                       MCP server (clients start ~/.local/bin/recall-mcp)
  recall serve                                     context over a local socket, for Hermes's memory
                                                   provider (recall-serve.service)

VAULT (encrypted on this machine; the assistant cannot read it)
  recall vault add <file|->  --title "Passport" [--note "..."] [--tag t]
  recall vault get "passport"      [--copy]        decrypt and show the best match
  recall vault list
  recall vault rm <source>

METRICS
  recall metrics collect           one system sample (recall-metrics.service does this every minute)
  recall metrics add sleep_hours 7.5 [--note ...]  manual entry
  recall metrics show [--since 24h] [--bucket hour] [--name sleep_hours]

HEALTH
  recall doctor [--alert]          check services, database, search, embeddings, vault key
                                   (recall-doctor.service checks every 10 min and alerts the phone)
  recall doctor --test-alert       send one test message to the phone
  recall eval [--save] [-v]        ask the questions in eval.toml and report how often search
                                   found the answer; --save keeps the run as the baseline

SETUP
  recall init                      create the vector index (once)
  recall stream purge [--yes]      drop noisy and repeated notifications (noise.toml)
  recall metrics init              create the time series collection (once)
  ~/.config/recall/sync.toml       folders and files for `recall sync` (contrib/sync.example.toml)
  ~/.config/recall/eval.toml       questions and their answers for `recall eval` (contrib/eval.example.toml)
  config: $RECALL_ENV, ./.env, ~/.config/recall/env, or the project .env

MORE
  recall <command> --help          flags for any command
  README: ~/Projects/recall/README.md
"""
