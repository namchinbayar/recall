# recall memory provider for Hermes

Makes recall Hermes's external memory: before each turn Hermes gets the few
items from recall that are relevant to the message (saved notes, memories,
facts learned from earlier chats, Claude session records, synced files,
earlier Hermes exchanges). Hermes's built-in MEMORY.md and USER.md keep
working as before.

It needs `recall serve` running (`recall-serve.service`) and nothing else: no
API key, no Python packages. With `recall-learn.service` running too, recall
reads each conversation after it goes quiet, keeps its lasting facts, and
maintains the profile this plugin puts in the system prompt.

```bash
cp -r contrib/systemd/recall-serve.service{,.d} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now recall-serve.service

cp -r contrib/hermes-memory/recall ~/.hermes/plugins/
# then in ~/.hermes/config.yaml:   memory:  provider: 'recall'
# and restart Hermes. To go back, put the old provider name there again.
```

Optional settings in `~/.hermes/recall.json`:

| key | default | meaning |
|---|---|---|
| `limit` | 4 | most items added to a turn |
| `min_score` | 0.55 | reranker score an item needs (0 to 1); raise it if unrelated notes show up |
| `max_chars` | 400 | length of each excerpt |
| `timeout` | 3.0 | seconds to wait for recall before the turn goes on without it |
| `include_events` | true | also recall events: past Hermes exchanges, minus the conversation that is asking |
| `exclude_tags` | `["hermes-memory", "desktop-notification", "digest"]` | never recall items with these tags (notifications and hourly digests are events too) |
| `learn` | true | hand each finished exchange to recall, whole, for `recall learn` |
| `profile` | true | put recall's profile of you in the system prompt |
| `socket` | `$XDG_RUNTIME_DIR/recall.sock` | which `recall serve` to ask; a private profile points at its own |
| `url` | empty | the hosted recall's `/serve` address, for a Hermes that cannot reach the socket (one in the cloud); used instead of `socket` |
| `key_env` | `RECALL_AGENT_KEY` | the environment variable holding the agent key for `url` (main README, "Online") |
| `store_turns` | false | hand each finished exchange to that socket to keep (the instance must set `RECALL_SERVE_STORE`); for a profile with no `recall-events` hook |

In a named profile the file is `<profile home>/recall.json`. Hermes imports a
plugin once per gateway process, so after changing the plugin's code restart
the gateway itself (`hermes gateway restart`), not only the profile.

In one conversation an item is shown once, and a short follow-up is searched
together with the message before it.

What it does not do: it does not feed the event stream (the `recall-events`
gateway hook stores every exchange there) and adds no tools (recall's MCP
server provides search, remember and save_note). Scheduled runs and
sub-agents get no recall and are not learned from.
