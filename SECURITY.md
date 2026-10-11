# Security Policy

`git-remote-seafile` handles two things worth protecting: the contents of your repositories, and a
Seafile **account token** that is a bearer credential for every library that account can reach. This
document describes how credentials are handled, what the helper does and does not defend against,
and how to report a problem.

The protocol-level threat model — what the lock guarantees, what a malicious server can do, and what
the wire protocol assumes — is in [DESIGN.md](DESIGN.md) §6. This file covers deployment and
reporting.

---

## Reporting a Vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private vulnerability reporting on
[tkittich/git-remote-seafile](https://github.com/tkittich/git-remote-seafile)
(*Security* → *Report a vulnerability*). If that is unavailable, open a minimal issue asking for a
private channel and include no details.

Please include: the version (`git-remote-seafile version`), the platform, the command that triggers
the problem, and what you expected instead. A proof of concept is welcome but not required.

This is a small project maintained by volunteers; there is no bug bounty and no formal SLA. What you
can expect is an acknowledgement, an honest assessment of whether the report is in scope, and a
fix or a documented limitation. Credit is given in the release notes unless you ask otherwise.

---

## Supported Versions

Only the most recent release receives security fixes. The project is pre-1.0 and does not maintain
back-branches.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Anything older | No — upgrade first |

---

## Credentials

### Where a token can come from

The helper resolves credentials through three sources, tried in this order and stopping at the first
that yields **both** a server and a token:

1. **Environment variables** — `SEAFILE_SERVER` and `SEAFILE_TOKEN`.
2. **Configuration file** — `~/.git-seafile.json`, a plain JSON file with `server` and `token` keys.
3. **The local Seafile desktop client** — the `accounts.db` SQLite database written by the client,
   read read-only.

Nothing else is consulted, and no credential is ever written by the helper.

### A token is only sent to the server it names

This is the single most important property here, and it is enforced rather than advised:

- **`SEAFILE_TOKEN` is used only when `SEAFILE_SERVER` is also set**, and only when that server
  matches the server in the remote URL. A bare `SEAFILE_TOKEN` is **ignored**, with a message on
  `stderr` naming the omission.
- **The `token` key in `~/.git-seafile.json` is likewise used only when `server` is present** in the
  same file and matches.

The reason is a confused-deputy attack that this project shipped and then fixed. A URL is
attacker-chosen — it can arrive from a README, a submodule, a CI variable, or a colleague — while the
credential is not. Adopting a bare token for whatever host the URL happened to name meant that
`git clone` against a host the user had never heard of posted their Seafile token to it on the first
request. Requiring the server to be named explicitly makes the two halves agree before anything
leaves the machine.

The practical consequence for CI: **set both variables**. A job that exports only `SEAFILE_TOKEN`
will fail to authenticate, by design.

### Plain HTTP

A token sent over plain `http://` is readable by anything on the network path. Because the scheme is
the user's choice in the URL, the helper **warns on every adoption** rather than refusing — naming
the server the token is about to travel to, on `stderr`. Loopback is exempt: a desktop client on
`http://localhost` is not on a network path, and warning on every operation there would train users
to ignore the message.

If you see that warning against a remote server, use `https://`.

### File permissions

`~/.git-seafile.json` is **plaintext**. On POSIX systems the helper checks the file mode and warns if
it is accessible to anyone but you; the fix is:

```bash
chmod 600 ~/.git-seafile.json
```

On Windows the file inherits the ACL of your user profile; no separate check is performed. If you
would rather not keep a long-lived token on disk at all, prefer the desktop client's `accounts.db`
(the helper reads it read-only) or a CI secret injected as environment variables.

### Credentials on a shared machine

Anything that can read your environment or your home directory can read your token, and a Seafile
token typically grants read/write to every library the account can see — not merely the repository
being pushed to. Treat it as you would an SSH private key. Revoking it is done from the Seafile Web
UI (*Settings* → *API Token*).

---

## Transport Security (TLS and proxies)

The helper makes **no TLS decisions of its own**. There is deliberately no `verify` option, no
`--insecure` flag, and no configuration key that disables certificate validation: a switch like that
is only ever reached for by someone who has already decided to stop checking, and it would then
apply to every subsequent operation.

That means certificate trust and proxying come entirely from the environment, via the standard
variables the underlying HTTP library honours:

| Variable | Effect |
|---|---|
| `REQUESTS_CA_BUNDLE` | Path to a CA bundle file. The usual answer for a **self-hosted instance with a private CA**. |
| `SSL_CERT_FILE` | Path to a CA bundle, honoured as an alternative to the above. |
| `HTTPS_PROXY` / `HTTP_PROXY` | Proxy URL for outbound requests. |
| `NO_PROXY` | Comma-separated hosts that bypass the proxy — **set this to include your Seafile host** if it is on the internal network. |

Example, for a self-hosted server with an internal CA:

```bash
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/company-ca.pem
export NO_PROXY=seafile.internal.example.com
```

Two notes on why this is the right shape:

- **A private CA is the common case for self-hosted Seafile**, not an edge case. Pointing
  `REQUESTS_CA_BUNDLE` at the CA is the supported path; the unsupported path is disabling
  verification.
- **Git's own `http.sslCAInfo` is not read.** The helper is a separate process from `git-http-fetch`
  and does not inherit Git's TLS configuration, so a setup that works for a GitHub remote may still
  need the environment variable here. This is a frequent source of confusion and is worth checking
  first when a self-hosted server fails with a certificate error.

---

## Supply Chain

### The dependency floor

`git-remote-seafile` has exactly one runtime dependency, and `pyproject.toml`
requires **`requests>=2.32.4`**. That is a security floor, not a feature one. The
helper holds a Seafile token and runs inside every `git` invocation, so the
transport library's own credential handling is part of this tool's attack
surface — and each of the versions below closed a leak in it:

| Version | CVE | What it fixed |
|---|---|---|
| 2.31.0 | CVE-2023-32681 | The `Proxy-Authorization` header followed a redirect to a different host. |
| 2.32.0 | CVE-2024-35195 | `verify=False` set on a `Session` persisted into later requests made through that same `Session`. |
| 2.32.4 | CVE-2024-47081 | A crafted URL could leak `.netrc` credentials to a third party. |

The floor is *tested* rather than merely declared: the `min-deps` job in
`.github/workflows/ci.yml` pins exactly that version, installs it with
`--no-deps` so the resolver cannot quietly upgrade past it, and runs the whole
suite against it. Every other CI leg installs the latest release, so without
that job nothing would exercise the number this package claims to support.

Dependabot watches the workflow actions and deliberately does **not** raise this
floor. A bot moving a security floor is not a security review.

### Verifying a release artifact

Releases are published as GitHub Release assets: a wheel, an sdist, and a
`SHA256SUMS` file. Two checks are available, and they answer different
questions.

**Integrity — is this the file that was published?**

```bash
sha256sum --check SHA256SUMS     # macOS: shasum -a 256 -c SHA256SUMS
```

Run it in the directory holding both the checksum file and the downloads.

**Provenance — who built it, and from which commit?**

Each artifact also carries a build-provenance attestation, signed by the release
workflow's own identity:

```bash
gh attestation verify git_remote_seafile-<version>-py3-none-any.whl \
  --repo tkittich/git-remote-seafile
```

(`gh` needs to be recent enough to have the `attestation` command; a checksum is
the fallback when it is not.)

A checksum cannot answer the second question — anyone able to replace the file
can replace the checksum sitting beside it. The attestation records the
repository, the workflow, the commit and the digest, which is why it is the
check worth running for a program you are about to hand a token to.

---

## Threat Model

### What the helper protects against

| Threat | Mitigation |
|---|---|
| Credential disclosure to an unintended host via a crafted URL | A token is only used when its server is named and matches the remote (above) |
| Credential disclosure over a plaintext link | A warning on every adoption of a token over `http://` to a non-loopback host |
| A file in the working directory shadowing an import | Launchers run Python in isolated mode (`-I`), so the current directory is not on the import path |
| A malicious or corrupted packfile installed locally | Strict packfile-name validation, `verify-pack -v`, and size verification against the remote listing |
| A server answering success for an upload that failed | Compaction re-lists the remote and requires every new packfile at its expected size before deleting anything |
| A stale lock holder writing inside a live holder's push | Ownership fencing before every destructive step, detecting both takeover and preemption |
| Silent data omission in a snapshot | The snapshot tool's excludes are opt-in and named, never silent |
| A substituted or mislabelled release download | Every release ships a `SHA256SUMS` file and a build-provenance attestation signed by the release workflow (above) |

### What it does not protect against

- **A malicious server operator.** They hold your content and can serve whatever they like.
  Validation detects a *corrupted* packfile, not a deliberately crafted history.
- **A compromised account or a second client using the same credentials.** The lock is advisory and
  per-account; two machines on one account are not distinguishable from each other.
- **An attacker with write access to the library.** They can delete refs and packfiles, or plant a
  lock queue that changes who wins an acquisition.
- **Client-side encryption.** Content is sent to the server as-is. If you need encryption at rest
  from the client's perspective, put [`git-remote-gcrypt`](https://spwhitton.name/tech/code/git-remote-gcrypt/)
  in front of this helper.
- **Traffic analysis.** The server sees which repository is pushed to and when.
- **A hostile `~/.git-seafile.json` or `accounts.db`.** If an attacker can write those, they can
  already act as you.

### What the lock queue discloses

The lock is a set of JSON tickets in `.git-lock.d/` **inside the library**, readable by anyone with
access to it. Each ticket contains:

- a hash derived from the account token (not the token itself),
- a machine identifier,
- the holding process's PID,
- a nonce (which is also the filename),
- timestamps and a lease expiry.

This is not a secret store and should not be treated as one. It discloses which machines push to a
repository and roughly when. It does **not** disclose the token, and the hash is not usable as a
credential.

### Read-only operations

`lock-status` is strictly read-only: it never reaps, rewrites, or mutates tickets. Diagnosis is safe
to run at any time, including while another client holds the lock.

---

## Hardening Checklist

- [ ] Use `https://` for every remote. Treat a plain-HTTP warning as an error to fix.
- [ ] In CI, set `SEAFILE_SERVER` **and** `SEAFILE_TOKEN` together, from the CI secret store.
- [ ] `chmod 600 ~/.git-seafile.json` if you keep a token on disk.
- [ ] For a self-hosted instance, point `REQUESTS_CA_BUNDLE` at your CA rather than disabling
      verification (which is not possible anyway).
- [ ] Set `NO_PROXY` to include an internal Seafile host when a proxy is configured.
- [ ] Prefer a token scoped to the account you actually need; a Seafile token is not per-library.
- [ ] Do not disable the pre-flight safety checks (`seafile.skipsafetychecks`) outside throwaway
      test environments — they are what stops a push from writing packfiles over an active working
      tree.
- [ ] Run `check-auth` after changing credentials, to confirm the helper resolves the server and
      token you intended rather than a different tier's.
