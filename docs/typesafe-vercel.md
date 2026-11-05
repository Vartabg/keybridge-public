# Connect TypeSafe/Jev to a Vercel site

Run setup in your own Terminal. It selects a Bitwarden item without showing its
key, checks TypeSafe once, and saves `TYPESAFE_API_KEY` as a sensitive variable in
the linked Vercel project. Agents can then inspect metadata only.

## Before setup

- Python 3.11+, the official Bitwarden CLI (`bw`), and the official Vercel CLI
  (`vercel`) must be installed. Sign into Vercel and link the site first.
- Save the TypeSafe key in Bitwarden as a login password, custom field, or
  secure note containing only the key. Naming the entry TypeSafe or Jev helps
  the private picker find it. Do not send a key through chat.
- Keybridge uses `~/.local/share/keybridge/bitwarden`, a dedicated CLI profile.
  For EU or self-hosted Bitwarden, configure `bw config server` with
  `BITWARDENCLI_APPDATA_DIR` set to that path before running setup. Otherwise the
  CLI's default server is used. First use signs in privately with `bw login
  --quiet`; subsequent uses need only the vault unlock.

## Owner command

From the Keybridge checkout, in a private Terminal:

```sh
python3 -m keybridge connect-typesafe \
  --site /absolute/path/to/linked/site \
  --target preview --target production
```

Preview is the default when no target is supplied. The command displays the
verified project, account and environments before asking for the vault unlock.
Progress is shown before each potentially slow step:

1. Verify the Vercel project and existing connection, with the vault closed.
2. Privately sign in or unlock Bitwarden and select the credential.
3. Lock Bitwarden and check TypeSafe once.
4. Recheck the destination and send the key to Vercel.
5. Verify the saved variable's metadata.

### What to enter at the password prompt

After first-time sign-in and any email verification, Keybridge locks the vault
and asks for your **Bitwarden master password once more** to open it for this
connection. Enter the same master password used during sign-in. The emailed
verification code belongs only at Bitwarden's earlier verification prompt.

At `Bitwarden master password (hidden input):`, type the password and press
Return. No characters or dots appear while you type; this is expected. An empty
entry stops setup without sending an unlock request. If unlock is rejected,
setup stops before selecting or transferring a TypeSafe key. Rerun the same
setup command to try again; if sign-in is still valid, it is skipped.

Choose your TypeSafe vault item and field by number. Keybridge remembers that
reference, locks its vault profile, checks the credential, and sends the key to
Vercel. The check uses a fixed sentence about reading a book with `jev-1.13.0`;
normal TypeSafe charges can apply. It sends no research or personal text.

On macOS, `scripts/connect-typesafe.command` can be opened in Terminal. With no
arguments it asks for a linked site path and uses Preview. It also accepts the
same command options for a prepared launcher.

The key travels to TypeSafe in an authentication header and to Vercel over the
authenticated CLI's stdin. It is never printed, written to a local plaintext
file, or placed in command arguments. The private local receipt contains only
the vault reference and connection metadata. This does not create an OS
security boundary against another program running as your user.

## Check and activate

This command is safe for agents and never opens the vault:

```sh
python3 -m keybridge deployment-status \
  --site /absolute/path/to/linked/site \
  --target preview --target production
```

Use the same targets as setup. `managed_connection: true` means the recorded
write matches Vercel's sensitive-variable metadata. `provider_checked_at` is
the time of the last successful check, not a continuous health test. A **new
deployment is required** to activate an environment-variable change. The owner
flow does not deploy or prove that the site works. Verify a fresh Preview before
publishing. The local broker's lock, quota and expiry do not affect cloud use.

## If setup stops

- If stage 2 cannot find `bw`, compare `command -v bw` in your own Terminal
  with the supported locations in the [README](../README.md). A lookup failure
  can mean the installation is outside the search path; it does not prove the
  CLI is missing. The per-user npm prefix `~/.npm-global/bin` is supported.
- The numbered stage identifies where setup stopped. A failed read-only Vercel
  check did not itself change any variables. If a write was already accepted
  and the final check fails, the message explicitly preserves that uncertainty
  and directs you to `deployment-status` before another transfer.
- A rejected key or invalid provider result causes no Vercel write. Correct the
  selected Bitwarden item, then rerun; use `--choose-again` to change the reference.
- An existing variable, different metadata marker, or changed variable ID
  stops setup. Review it in Vercel; this flow never replaces existing values.
- After an interrupted write, rerunning first checks the remote marker. A
  matching variable completes recovery without another unlock, provider call
  or write. If no variable is visible, it stops. Only after checking Vercel and
  confirming the variable is absent should the owner use `--retry-unconfirmed`.
- Repeating a completed setup makes no paid check or new write.
- To revoke cloud access, revoke the key at TypeSafe, or remove the Vercel
  variable and replace deployments that received it. `keybridge lock` only
  controls the separate local broker session.

The first private owner activation and a live Preview search passed on
September 25, 2026.
Each new connection still requires its owner's private activation; one successful
query does not establish general routing accuracy or production readiness.

Contracts checked September 24, 2026: [TypeSafe API](https://docs.typesafe.ai/api),
[Vercel environment-variable API](https://vercel.com/docs/rest-api/projects/create-one-or-more-environment-variables),
and [Bitwarden CLI](https://bitwarden.com/help/cli/).
