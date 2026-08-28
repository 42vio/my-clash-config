# Stable AmyTelecom provider design

## Goal

Replace the current owner-only airport-node expansion with a stable, owner-only
`AmyTelecom.yaml` endpoint.  An administrator supplies a short-lived upstream
Clash YAML URL only when updating the airport.  The service downloads that
file once, preserves its bytes unchanged, and publishes a durable URL under
the owner's existing bearer token:

```text
https://<subscription-authority>/s/<owner-token>/AmyTelecom.yaml
```

Owner profiles use this URL as a Mihomo `proxy-provider`; airport nodes must
not be expanded into the generated profile's `proxies` list.  The same stable
URL must work as a standalone airport subscription/provider source.

This design applies only to the owner.  Member profiles continue to contain
only their own 3x-ui nodes and must not reveal the airport URL, name, or
content.

## Decisions

### Release-bound raw airport YAML

The recommended implementation is to attach the untouched airport YAML to an
owner release, rather than maintain one globally mutable public file.

* A successful airport update creates a candidate owner release containing
  the three rendered profiles and the exact downloaded `AmyTelecom.yaml`
  bytes.
* The corresponding private release copy and public static release copy have
  the same content digest.  The existing release integrity checks must verify
  this additional artifact, its expected mode, ownership, and absence of
  symlinks/hard links.
* The exact stable Nginx route aliases the airport file in the owner's current
  release.  Its URL therefore does not change between airport updates, while
  its target advances atomically with the owner release.
* Owner rollback also changes the route target back to that release's airport
  file.  A rollback can never pair an old profile with a newer airport file.

Keeping a separate mutable public airport file was rejected because it would
break that rollback guarantee.  Reusing the five-minute upstream URL was
rejected because it expires and cannot be revoked when this service rotates a
token.

### Generated owner configuration

For every owner variant, emit one provider named `AmyTelecom`:

```yaml
proxy-providers:
  AmyTelecom:
    type: http
    url: https://<subscription-authority>/s/<owner-token>/AmyTelecom.yaml
    path: ./proxy_providers/AmyTelecom-<airport-content-digest>.yaml
    interval: 0
```

The `path` digest changes only when the raw airport file changes.  This makes
a client that refreshes the primary profile use a new local provider cache and
download the current stable airport YAML.  `interval: 0` deliberately disables
background provider pulling; the service does not introduce a timer or a
periodic client-side provider refresh.  Mihomo only starts an HTTP provider
pull loop when the interval is positive, and otherwise uses an existing local
cache first.  The digest path is needed to obtain the requested refresh
behaviour without periodic polling.  See the [Mihomo fetcher
implementation](https://github.com/MetaCubeX/mihomo/blob/v1.19.30/component/resource/fetcher.go)
and [provider documentation](https://wiki.metacubex.one/en/config/proxy-providers/).

Airport-capable groups must reference the provider through `use:
[AmyTelecom]`, not by copying airport node names into `proxies`.  The generator
must inject that use-list only into the owner groups that currently receive
airport nodes, including groups that presently rely on `include-all` for
airport nodes.  The home feature follows the same rule for its all-node group.
Home nodes and 3x-ui nodes retain their existing inline behavior.

Member profiles have no `proxy-providers` section and no `use: [AmyTelecom]`
reference.  No general template or variant manifest may grant this provider to
a member.

### Stable endpoint and authorization

`render_routes` creates an additional exact owner route:

```text
/s/<owner-token>/AmyTelecom.yaml
```

It has the same authorization and request hardening as a profile route:

* only `GET` and `HEAD`, no query string, no generic `/s/` fallback;
* rate/body limits, `access_log off`, `log_not_found off`, YAML content type,
  `nosniff`, and no-store response semantics;
* a download name and profile title of `AmyTelecom`;
* no `Subscription-Userinfo` header, because airport traffic is not 3x-ui
  traffic;
* no route for members, disabled/deleted owners, owners without a current
  airport release, unknown tokens, or readable-code-only paths.

The full owner token is intentionally present only in that owner's published
profiles as the provider URL.  It remains a bearer secret: it must not appear
in logs, status output, Git, errors, or other users' files.  Validation must
allow only this exact expected provider URL in an owner profile; it must still
reject every other user token, loopback source URL, 3x-ui subId, and transient
upstream URL.

### Update, sync, rotation, and rollback lifecycle

1. **Update airport subscription**: accept the short-lived source URL through
   the existing interactive prompt.  Download at the existing bounds, retain
   the exact response bytes, parse only enough to confirm that it is a valid
   non-empty Clash proxy-provider YAML, and validate it using a temporary
   local-file provider configuration.  Do not normalise, dump, rename, or
   otherwise rewrite the downloaded file.
2. Render owner profiles with the provider URL and digest cache path, validate
   the candidate configuration, and validate through the pinned Mihomo binary
   using a non-published local-file equivalent of the provider.  The published
   profile remains HTTP-provider based.
3. Publish the candidate airport file, owner releases, state, current markers,
   and routes in the existing atomic activation transaction.  Any fetch,
   parse, render, validation, Nginx-test, or reload failure leaves the old
   airport file and owner profiles live and discards the candidate.
4. **Sync all**: never re-fetch the expired upstream URL.  It loads the raw
   airport file from the current verified owner release and reuses it while
   regenerating owner profiles.  Before the first successful airport update,
   the owner remains pending as today.
5. **Rotate owner link**: create a new owner release using the same raw airport
   bytes but the newly generated token in the provider URL.  Atomically switch
   the profile routes and `AmyTelecom.yaml` route.  The old token immediately
   loses access to both.
6. **Rollback owner release**: route the stable URL to the selected release's
   raw file and restore the matching profiles.  Rollback of member-only
   releases remains unchanged.

## Required code boundaries

* `clash_sub/sources.py`: add a byte-preserving airport downloader/validator;
  remove the airport-specific normalisation/write-snapshot path once no
  caller needs proxy dictionaries.
* `clash_sub/generator.py` and templates: model the fixed `AmyTelecom`
  provider separately from inline sources, emit its expected mapping for
  owners only, and inject provider `use` references through explicitly
  declared template controls.
* `clash_sub/checks.py`: validate proxy-provider mappings and group `use`
  references, while keeping strict member isolation and exact expected-owner
  URL validation at the service boundary.
* `clash_sub/release_store.py`: store, verify, read, and expose the raw
  owner-release airport artifact without changing its bytes.
* `clash_sub/service.py`: make airport update, sync, owner rotation, and
  owner rollback use the release-bound artifact and one atomic activation.
  Retire private `airport.yaml` snapshot assumptions.
* `clash_sub/nginx.py`: render the additional exact protected
  `AmyTelecom.yaml` route and safely resolve its release artifact.
* `clash_sub/cli.py`, README, operations, recovery, and private-data docs:
  retain the daily menu wording where appropriate but document the new stable
  endpoint, byte-preserving behavior, provider-only refresh model, and backup
  layout.

No source subscription URL is persisted.  No runtime service, background
refresh timer, online converter, new user option, or member airport access is
introduced.

## Verification plan

Tests must cover the following before integration:

1. The downloaded airport response is served byte-for-byte as
   `AmyTelecom.yaml`; comments, formatting, and YAML ordering are not
   rewritten.
2. Owner variants contain exactly one `AmyTelecom` HTTP provider, its stable
   owner URL, `interval: 0`, a content-digest cache path, and required
   provider `use` references.  Airport node definitions are absent from their
   `proxies` lists.
3. Member variants contain no airport provider, URL, node name, provider
   reference, or raw airport content.
4. A changed airport body produces a new provider cache path; an unchanged
   body does not create unnecessary content changes.
5. The Nginx route is exact, owner-only, query-free, anonymous in generated
   text, and has the expected safe headers.  It never emits a false traffic
   header.
6. Airport update activation failures restore the old raw file, owner
   profiles, state, current markers, and route.  Failed candidates are not
   reachable.
7. Owner token rotation regenerates provider URLs and revokes the old airport
   endpoint.  Owner rollback restores a matching raw file and profile.
8. `sync_all` does not contact an upstream airport URL and instead reads the
   current verified release artifact.
9. The full unit suite, template/Mihomo validation path, repository safety
   scan, and documentation assertions pass.  Existing user changes outside
   this work, including `DEPLOYMENT.md`, remain untouched.
