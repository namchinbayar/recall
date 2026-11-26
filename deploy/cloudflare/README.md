# recall online on Cloudflare

A Worker (`src/index.js`) in front of one container that runs `recall mcp-online`
(`Dockerfile`, built from the repository root). The Worker forwards MCP requests
unchanged; recall checks the sign-in. A timer keeps the container running.

For the recall app the Worker also does the sign-in (`src/session.js`):

- `GET /auth/login` sends the browser to the authorization server (code flow with
  PKCE). The Worker is a public client with no secret, identified by the document it
  serves at `/oauth/client.json` (a Client ID Metadata Document), so nothing is
  registered by hand.
- `GET /auth/callback` exchanges the code, asks recall whether it accepts the user,
  and stores the tokens in the Durable Object's storage. The browser gets a random
  session id in an HttpOnly cookie, never a token.
- `/api/*` requests with that cookie are forwarded with the session's access token,
  refreshed when it is about to expire. A refused refresh ends the session; when the
  sign-in service only fails to answer (5xx, 429) the session is kept and the request
  gets a 503 to try again. Requests that change something must carry the
  `X-Recall-App` header and come from this site. A request with its own
  `Authorization` header is forwarded as it is.
- `POST /auth/logout` ends the session and revokes its refresh token.
- The iPhone app (`the companion iOS app`) signs in through the system sign-in sheet:
  `GET /auth/login?app=<challenge>` runs the same sign-in, but the callback sends the
  sheet to `com.example.recall://signed-in?code=…` with a one-time code instead of setting
  the cookie. The app posts that code and the verifier behind its challenge to
  `POST /auth/app/exchange` (with the `X-Recall-App` header) within two minutes and gets
  the session id, which it then sends as the `__Host-recall` cookie. A wrong verifier
  ends that session; the tokens never leave the Worker.

The app itself is the files in `app/`, served by Cloudflare as static assets (the
`assets` block in `wrangler.jsonc`) without running the Worker. Every page address
gets `index.html`; the paths under `run_worker_first` (the MCP endpoint, `/api`, the
sign-in) always go to the Worker. `app/_headers` sets the security headers: the pages
load nothing from other sites and run no inline script. `app/sw.js` keeps the app's
own files on the device so it opens offline; it never stores what recall returns.

```bash
node preview.mjs     # the app on http://127.0.0.1:8793 with sample data and no sign-in
node preview.mjs --api http://127.0.0.1:8791 --token <token>   # against a real recall server
```

`node --test deploy/cloudflare/test/*.js` runs the sign-in tests and checks that the app's
files, the offline list and the Worker's paths agree.

Settings are Worker secrets, none are in this repository:

| Name | What |
|---|---|
| `RECALL_ONLINE_URL`, `RECALL_ONLINE_ISSUER`, `RECALL_ONLINE_SUBJECTS` | as in the main README, "Online" |
| `MONGODB_URI` | the cluster address without a login |
| `MONGODB_USERNAME`, `MONGODB_PASSWORD` | a database user limited to the recall database |
| `VOYAGE_API_KEY`, `VOYAGE_MODEL` | embeddings |
| `RECALL_AGENT_KEYS` | `name:<sha256 of the key>` per agent key (not secret: only hashes); main README, "Online" |

The container's outbound address is not fixed, so the database's IP access list
must allow any address; the database user's password is then the lock.

```bash
wrangler secret put MONGODB_PASSWORD --name recall-online   # typed, not stored anywhere else
wrangler secret put VOYAGE_API_KEY --name recall-online
# the vault key, so the signed-in owner can use the vault_* tools (read from the desktop, never typed or printed)
wrangler secret put RECALL_VAULT_SECRET --name recall-online < ~/.config/recall/vault.key
./deploy.sh        # builds with podman, pushes the image, deploys the Worker
curl https://recall.example.com/healthz    # {"ok":true}
```

`deploy.sh` uses podman, so no root daemon is needed. wrangler's own build works only
with Docker: with podman the pushed image gets a different digest than the one wrangler
asks Cloudflare for, so the script pushes the image itself and deploys by that digest.
`podman-for-wrangler` is a wrapper for `wrangler deploy --dry-run` and local checks.

`deploy.sh` deploys only a clean checkout of main, so what runs is always a commit
on main and one branch's deploy cannot replace another's work (merge first;
`RECALL_DEPLOY_ANY_BRANCH=1` skips the branch check in an emergency). It sets
`RELEASE` to that commit, and a running container is replaced when `RELEASE`
changes, so a new commit restarts it with the new image. After changing only a
secret, `RECALL_RESTART=1 ./deploy.sh` restarts it on the same commit. The deployed
version carries the commit as its tag: `wrangler deployments list --name recall-online`.
