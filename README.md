# battlenet.nix

Experimental Nix overlay for pinned Battle.net installations and isolated Wine
runtimes, with an experimental native macOS runtime. Downloads come from Blizzard's CDN. This repository contains source
and generated Nix content records, not game binaries or account state.

A game is exported as a flake package only after its complete installation has
been reconstructed and hashed. Published games can be enabled with
`programs.battlenet.games.PRODUCT.enable = true;`.
**Only Warcraft III has been launched through the Battle.net login screen.
Account login, gameplay and the other games' Linux compatibility are unverified.**
Other catalog products are being initialized; metadata alone does not export a
game package.

## macOS / Apple Silicon

The flake also targets `aarch64-darwin`, using Blizzard's native macOS
files and an actual executable inside the game app bundle. Darwin does not
use Wine or DXVK. Product codes and module options stay the same:

```nix
{ inputs, ... }: {
  imports = [ inputs.battlenet.darwinModules.default ];
  nixpkgs.config.allowUnfree = true;
  programs.battlenet = {
    enable = true;
    games.s1.enable = true;
  };
}
```

Native catalogs are initialized separately as `games/PRODUCT.mac.nix`;
only complete, reconstructed and hashed catalogs export a Darwin package.
The updater discovers macOS distributions for Warcraft III (`w3`), StarCraft II
(`s2`), StarCraft (`s1`), Heroes (`hero`), Hearthstone (`hsb`), Diablo III (`d3`)
and the three WoW products. Initial catalogs and additional languages may still
be pending. Diablo II Resurrected, Diablo IV and Overwatch are excluded because
no native macOS game payload was found in their selected manifests.

An Intel-only macOS client needs [Rosetta](https://support.apple.com/en-gb/102527)
on Apple Silicon; macOS distribution does not imply an arm64 executable.
The updater verifies `CFBundleExecutable` and records architectures from the
hashed Mach-O itself. On APFS, launch projections use `clonefile` for private
copy-on-write storage, falling back to copying on other filesystems. Signed
application bytes remain unchanged. Native preferences may also live in the
user's Library outside the disposable game projection.

**Native macOS gameplay, login and app-signature acceptance have not been
verified on a Mac.** Evaluation and byte-level reconstruction can be checked
on Linux; they do not establish that a game runs on macOS.

## NixOS

```nix
# flake input
inputs.battlenet.url = "github:DzmingLi/battlenet.nix";
```

```nix
# Pass inputs through specialArgs.
{ inputs, ... }: {
  imports = [ inputs.battlenet.nixosModules.default ];
  nixpkgs.config.allowUnfree = true;
  programs.battlenet = {
    enable = true;
    games.w3.enable = true;
  };
}
```

The game entry supplies defaults from `games/w3.nix`: release, complete content
records, final installation hash, executable and arguments. Ordinary users do
not supply lock files. Additional complete game records automatically supply
module defaults under their product codes and flake packages.
Each game's `enable` defaults to `false`; an empty game entry or settings alone
do not install it. The global `programs.battlenet.enable` must also be enabled.

Language and service region are separate options:

```nix
programs.battlenet.games.wow_classic_era = {
  enable = true;
  locale = "enUS";
  region = "eu";
};
```

`locale` selects a complete hashed installation containing that language and
manages known client preferences where supported. A separately verified variant is
stored as `games/PRODUCT.LOCALE.nix`; missing variants fail evaluation rather
than silently using the wrong resources or downloading updates at runtime.
The module currently applies client language preferences for Warcraft III,
StarCraft II, Heroes of the Storm and WoW products. Other games can select
verified language resources too, but may still require a language choice in the
client; automatic client preference adapters remain unverified for those games.
`locale = null` (the default) uses
the bundled installation without managing client language preferences.

`region = "cn"` selects a separate verified China distribution. WoW products
also support `"us"`, `"eu"` and `"cn"` through `WTF/Config.wtf`'s `portal` key.
Other clients handle authentication themselves; non-CN service preferences for
those games still require an adapter and reject explicit selections.
`region = null` (the default) leaves the client's selection unchanged.
Managed settings are applied before each launch; other settings remain intact.
Client login, availability and the resulting in-game selection still require
actual account testing. Changing language selects a different installation and
therefore a different version-isolated prefix; user-state migration is not
implemented.

China uses a separate verified distribution rather than the international
client's Chinese language variant:

```nix
programs.battlenet.games.wow_classic_era = {
  enable = true;
  region = "cn"; # Defaults to zhCN and requires a complete China catalog.
};
```

China catalogs are stored as `games/PRODUCT.cn.LOCALE.nix`. The updater selects
the official CN versions/CDN row, validates the product configuration, replaces
existing region tags with `CN`, and includes official country-specific tags
(such as WoW's `Alternate`). Diablo III maps the logical `d3` entry to Blizzard's
separate `d3cn` product and launcher name; Diablo II maps `osi` to the verified
China channel `osic`. Missing China catalogs fail evaluation;
an international `zhCN` installation is never substituted. China state and Wine
prefixes are isolated from international installations even when objects overlap.

For any game with a complete verified China catalog, `region = "cn"` selects
that distribution, its official executable/arguments and an isolated Wine/state
directory. The selected manifest supplies Chinese assets. SC2 additionally uses
its existing language preferences; WoW writes its `portal = "CN"` preference.
Other clients retain their official login flow; the wrapper does not invent
server flags or provide Battle.net/NetEase authentication tokens.
A CN version row is required: the generator does not invent `w3cn`/`s2cn` product
codes or assume every game has a current CN release. SC2 uses product `s2`, and
its CN build may differ from the international build.

The October 5, 2026 CN endpoint/configuration survey found:

| Product | Verified CN release | Installation/launch status |
| --- | --- | --- |
| `d3` (`d3cn`) | 2.8.1.101167 | Complete verified catalog published; official `x64/Diablo III64.exe` |
| `s2` | 5.0.14.93333 | Complete catalog generation in progress; CN manifest and 64-bit switcher selection verified |
| `hsb` | 36.6.3.253932.253216 | Complete catalog generation in progress; CN manifest and `Hearthstone.exe` selection verified |
| `fenris` | 3.2.2.73764 | CN configuration verified, including `Alternate` assets and official launch arguments; no complete catalog yet |
| `pro` | 2.24.1.1.153619N | CN configuration selects `NeacLoader.exe`; no complete catalog or Wine/anti-cheat validation yet |
| `hero`, `s1` | Historical CN version rows | No CN CDN row on the queried endpoint; initialization refuses to substitute international downloads |
| `osi` (`osic`) | 3.3.93854 | CN manifest and `D2R.exe` content verified; complete catalog generation in progress |
| `w3` | No CN version row for this code | Further distribution discovery required; international Chinese builds are not CN catalogs |

[Diablo II's official China site](https://d2.blizzard.cn/) confirms a CN service.
Its actual China release is advertised under [`osic/versions`](http://cn.patch.battlenet.com.cn:1119/osic/versions),
with [`osic/cdns`](http://cn.patch.battlenet.com.cn:1119/osic/cdns) selecting
`blzdist-d2r.necdn.leihuo.netease.com` and the shared CDN path `tpr/osi`.
The verified product configuration declares `product = "osic"`, `zhCN` as its
only supported locale, and `D2R.exe` with no additional launch arguments.
The Windows/x86_64/zhCN manifests select 145,298 download objects
(25,647,003,402 encoded bytes) and five loose files. `D2R.exe` was downloaded
and verified against its encoded and decoded content hashes; this does not
establish successful CN login or gameplay under Wine.

CN SC2 selects 253 loose files and 184,784 download objects (27,376,124,859
encoded bytes); CN Hearthstone selects 5,570 loose files and 5,561 download
objects (12,931,656,247 encoded bytes). Their selected launch executables were
also fetched and verified against official content hashes. These checks are not
full installation or gameplay tests. A game becomes selectable only after the bot
publishes a complete catalog with its final installation hash.

Mainland authentication uses the official NetEase/Battle.net account flow;
account binding, tokens and credentials are never placed in the Nix store.
Installation and preference support does not prove successful CN login or
Wine compatibility. See the [official login migration announcement](https://wow.blizzard.cn/news/20250623/40565_1242492.html)
and [China Battle.net login](https://account.battlenet.com.cn/login/).

Options remain overridable, for example `games.w3.args = [ "-launch" ];`.
A custom entry can supply `release`, `installation`, `executable` and `args` as
Nix values. An `installation` contains complete, object-hashed `storage`. Its
legacy directory `hash` is optional and can be `null`.
Alternatively, supply an immutable derivation as `content`; this disables the
bundled installation source. Metadata-only games require a complete installation
or custom content and launch settings before they can be enabled.

All Linux games share `pkgs.wineWow64Packages.unstable` and the binary output
of `pkgs.dxvk`, following the consumer's nixpkgs revision. The flake pins
nixos-unstable with DXVK 3 as its Linux default. Additional graphics backends
such as VKD3D are not configured. The earlier shared runtime was verified with
WC3; the updated DXVK and other games need actual gameplay testing.

## Play without the NixOS module

```sh
nix run github:DzmingLi/battlenet.nix#w3
```

This downloads the pinned Warcraft III installation if it is not already in the
store, then starts the game. The launcher command is `w3`. Choose this command or the NixOS module above;
ordinary players do not need the updater or metadata packages.

## Advanced overlay API

Apply `inputs.battlenet.overlays.default` to expose `pkgs.battlenet`.
`pkgs.battlenet.mkGames` accepts release/installation/content/launch settings
and returns launcher packages. The module filters enabled entries and removes
its `enable` flag before calling this builder. The flake helper is
`inputs.battlenet.lib.mkGames { inherit pkgs; games = { ... }; }`.

The lower-level `mkInstallation`, `mkSnapshot`, `mkGame`, `mkMetadata`,
`mkManifests`, `mkPlan` and `mkFile` helpers remain available, including their
existing file-based interfaces for custom tooling. The Python build backend
receives JSON generated internally from Nix data; these temporary transport
files are not separate user-maintained locks.

## Game data and updates

This section is for maintaining game records, not required to play a bundled
game. The updater is the repository's `update.nu` script, run directly by CI
inside a dedicated Nix environment. It is not exported as a package or installed
by the NixOS module.

`games/PRODUCT.nix` is the single committed record for each product. It contains:

- `release`: a pinned version and verified build/CDN configuration locations.
- `installation`, when available: complete encoded object records, official
  bootstrap manifests/indexes, full-byte object SHA-256 hashes and launch defaults.

Blizzard's version service, configuration files, manifests, indexes and CDN
objects are the source data. The Nushell updater resolves releases, orchestrates
the binary backend, and emits native Nix attribute sets. CDN configuration MD5
and SHA-256 checks run in Nu; BLTE/TACT/CASC verification stays in Python.
Nix string interpolation is escaped.
International downloads use `https://cdn.blizzard.com` as their common entry
point. The updater collapses Blizzard's global CDN aliases to this address;
Nix fetches and backend downloads also normalize URLs in older pinned catalogs.
China's official NetEase CDN addresses remain unchanged. This affects transport
only: pinned object hashes, archive offsets and existing installation metadata
are preserved, so changing the entry point does not invalidate object reuse or
existing installation hashes. Downloads still verify content and exact ranges;
the common hostname does not promise the fastest route on every network.
Complete catalogs reconstruct repeated CDN object URLs from their pinned
release inside the Nix expression. Alternative CDN URLs remain explicit; every
generated expression is evaluated back and compared with the full verified data.
This keeps large catalogs below GitHub's individual file size limit without
changing their evaluated contents or installation hashes.
The catalog tracks the following product codes. Only entries with a completed,
hashed installation are exported as game packages; metadata-only entries remain
available to the maintenance pipeline.

| Code | Game |
| --- | --- |
| `w3` | Warcraft III: Reforged |
| `s2` | StarCraft II |
| `s1` | StarCraft: Remastered |
| `wow` | World of Warcraft retail |
| `wow_classic` | World of Warcraft progression classic |
| `wow_classic_era` | World of Warcraft Classic Era |
| `d3` | Diablo III |
| `osi` | Diablo II: Resurrected |
| `fenris` | Diablo IV |
| `pro` | Overwatch |
| `hero` | Heroes of the Storm |
| `hsb` | Hearthstone |

```sh
# Report available versions without downloading full games or writing records.
nix develop .#ci --command nu --no-config-file update.nu --check

# Generate and verify updates; select one product or omit --product for all.
nix develop .#ci --command nu --no-config-file update.nu --product w3
```

For a complete profile, the updater retains its platform/language selection,
fetches new official manifests, reuses pinned hashes and downloads only objects
without prior full-byte hashes. It generates the complete storage description
without assembling a game directory. Existing launch settings are kept.
This does not establish that gameplay works with the updated client. Encrypted
resource blocks are preserved for the official client's key provider: their
encoded identity, chunk checksums and SHA-256 are checked, but their plaintext
content hash is explicitly marked unverified. Bootstrap manifests and loose
installation files must decode and pass their plaintext checks. Unsupported
formats or missing objects fail without publishing an update.
Unchanged products are skipped. A failed product prevents writing any staged
catalog updates.

The updater also supports preparing a product's first complete installation:

```sh
# Maintenance operation: downloads and reconstructs the selected game.
nix develop .#ci --command nu --no-config-file update.nu \
  --initialize --product hero

# Prepare the official native Mac distribution.
nix develop .#ci --command nu --no-config-file update.nu \
  --initialize --product s1 --platform mac

# Prepare an independently hashed language variant; preserves the base profile.
nix develop .#ci --command nu --no-config-file update.nu \
  --initialize --product wow_classic_era --locale zhCN

# Prepare the distinct China distribution (language defaults to zhCN).
nix develop .#ci --command nu --no-config-file update.nu \
  --initialize --product wow_classic_era --region cn
```

`s2`, `s1`, `hero`, `d3`, `osi`, `hsb` and `wow_classic_era` have installation defaults checked against their
official product configurations and selected installation manifests. Manual
workflow initialization therefore only needs the product code. `--tags`,
`--executable`, `--data-directory` and `--game-args` override these defaults;
launch argument overrides use a JSON string array. Products without verified
defaults require explicitly researched tags and an executable; for China, the
verified product configuration supplies the executable and launch arguments.
This also works when initializing a language/China variant of a metadata-only
record, without inventing manifest tags or publishing an incomplete installation.
Language initialization validates the locale against the official hashed
product configuration and substitutes only language tags. It does not infer
the game server from the requested language. Ordinary scheduled updates maintain
base profiles, language variants and China distributions; `--product` selects both for that game.

The daily `Update game catalog` workflow discovers supported locales
from verified official product configurations for games with complete installations.
It automatically initializes missing language catalogs and updates changed ones,
with separate jobs and direct bot commits for each language. Existing bundled
languages use the base catalog; China uses its own allowed locales and distributions.
Manifest selection, content verification and final installation hashing must
succeed before a language becomes selectable. Metadata-only games continue to
receive release updates; they need verified installation defaults and an initial
complete installation before automatic language expansion. An unchanged catalog
does not create a download job. The first expansion can take many large downloads;
availability grows as the jobs publish their verified catalogs.
Discovery failures become failed jobs for the affected game, while other games
continue. Metadata downloads retry transient transport/HTTP errors up to four
times and report each failed endpoint. Unchanged configuration keys reuse the
previously verified records from the same product and distribution; new keys
still require fresh content verification.

Large object catalogs are compressed native Nix records published as GitHub
Release assets, outside Git. `games/*.nix` contains small release/language
summaries plus content-addressed URLs and pinned unpacked NAR hashes. The loader
keeps those summaries available without downloading the full catalog; installation
evaluation fetches only the selected catalog's full object graph. This keeps the
flake source snapshot small while retaining individual fixed-output object inputs
and cross-version reuse. Assets are never overwritten. After each update run, obsolete assets are deleted;
only current main references and assets used or potentially being uploaded by
unfinished CI runs are retained. There is no previous-version or seven-day grace
period. Historical flake locks may therefore need updating before a fresh build.
The repository starts from a compact initial snapshot; ordinary bot updates append
commits without rewriting source history.
Initialization runs even if the pinned release is unchanged. Once its verified
installation data is committed, the flake exports the product's package, the
module supplies its defaults, and ordinary bot updates maintain it automatically.
The updater does not guess launch executables or certify Wine/gameplay support.
The GitHub workflow's manual dispatch exposes the same initialization settings;
daily scheduled runs continue maintaining all complete profiles automatically.

The GitHub Actions update workflow runs daily and can be dispatched manually.
Workflow runs are serialized; their catalog jobs still run in parallel.
It runs `update.nu` in the CI development shell, generates the catalog,
validates launcher profiles without instantiating the full object derivation graph,
and commits successful changes directly to `main` as `github-actions[bot]`.
There are no automatic pull requests. The workflow uses `contents: write`; a
repository rule that prohibits bot pushes must allow this workflow's commits.
Publication integrates current main in an isolated checkout and retries competing
pushes, never force-pushing. Catalogs changed by another publisher are retained.
Each initialization changes only its own game file.
Release cleanup preserves catalog references from all runs still eligible for
GitHub's 30-day re-run window, including the run doing the cleanup, and all
unfinished runs. Recent uncommitted uploads are retained for recovery too.
If an older catalog is unavailable, the updater skips that optional hash-reuse
source and downloads and verifies the required objects against official manifests.
Publication retries uploads and pinned-hash verification, reporting both errors
if the release asset remains unavailable.
Before publishing, the workflow saves the verified Nix catalog as a GitHub Actions
artifact for seven days. A Git conflict can therefore be resolved from that
artifact without downloading the complete game again.
It also saves a `prepared/PRODUCT.nix` checkpoint after resource verification,
before publication. Its installation directory hash is intentionally `null`;
object hashes already pin its contents. Recovery rechecks the official manifest
closure before publishing it as a complete profile.
Checkpoints are uploaded even after a reconstruction failure. A maintainer can
recover an interrupted first installation from the downloaded checkpoint:

```sh
nix develop .#ci --command nu --no-config-file -c \
  'use ./update.nu finish; finish /path/to/prepared/s2.nix'
```

Recovery verifies completeness against the pinned official manifests without game
payload downloads, then writes the game catalog. No installation directory or
installation NAR hash is needed. It refuses to overwrite an already
initialized product. Publishing the recovered catalog remains a separate step.

User installations fetch each encoded CASC object through its own fixed-output
Nix derivation, named by EKey and pinned by SHA256. Complete installations use
these objects plus pinned configuration files and CDN indexes as local inputs;
the assembly command does not download missing resources. Updating to a build
that shares objects reuses their store paths and downloads only absent objects.
The installation is assembled by a normal, network-isolated derivation. Its inputs
are individually SHA-256 pinned; historical installation NAR hashes remain in old
catalogs but are no longer required or enforced by assembly.

Game launchers retain the encoded object cache in their store closure, so normal
GC preserves reusable objects while the launcher is retained. Once all referring
launchers and generations are removed, GC may delete those objects; a later
installation then downloads them again. Encoded objects consume additional disk
space alongside the assembled installation. Assembly still writes a complete new
immutable game tree: this saves network transfers, not all local copying or disk
space. The first installation after switching from the old monolithic builder
needs to populate the object cache.

Large object closures also require a higher Linux sandbox mount limit: Nix
bind-mounts their store paths into each build sandbox, and the default limit of
100,000 mounts is below several game closures. The NixOS module sets
`boot.kernel.sysctl."fs.mount-max" = lib.mkDefault 2000000`. Before the first
build (including the first NixOS rebuild that enables this module), apply it
to the running kernel:

```sh
sudo sysctl -w fs.mount-max=2000000
```

Standalone overlay users must persist this setting through their system's
sysctl configuration. Exceeding this limit reports `No space left on device`
while setting up sandbox bind mounts even with free disk space. See the
[Linux mount-max documentation](https://kernel.org/doc/html/v6.0/admin-guide/sysctl/fs.html#mount-max).

Catalog updates download manifests and only objects with no previously pinned
SHA-256 record. The selected previous catalog and the product's Windows/macOS
base catalogs supply reusable records across builds, languages and platforms.
Reused objects retain their full-byte SHA-256 and receive the new manifest's CDN
location; user builds still verify every downloaded byte. CI discards newly hashed
payloads instead of assembling or downloading a full installation a second time.
First-time objects still require one complete download to compute SHA-256;
Blizzard EKeys cannot replace full-byte hashes for multi-block BLTE objects.
The downloader bounds outstanding requests to twice the worker count, avoiding
hundreds of thousands of queued futures for large games.

Publication retries concurrent Git pushes in an isolated checkout. If another
publisher changed the same catalog after a job's source revision, that remote
catalog is retained; subsequent discovery checks whether it needs another update.

The bundled China Classic Era catalog contains a complete verified installation
for 1.15.9.70003: 38 loose files and 147,488 download objects (5,039,053,478
encoded bytes). Including bootstrap resources, 147,726 objects share identical
EKeys and SHA256 hashes with the international `zhCN` catalog. Only four encoded
objects (77,019,106 bytes) are new to China. The regional `WowClassic.exe` and
`WowVoiceProxy-China.exe` were also independently extracted with CascLib and
match the official CN content hashes. A complete Nix sandbox build and a repeat
build with unusable download proxies reproduced the catalog NAR hash. This
verifies installation data, not account login or gameplay.

The October 5, 2026 manifest survey verified bootstrap manifests, archive
indexes, root metadata and the launch executable for `s2`, `hero`, `d3` and
`hsb`. This is a small partial download, not a complete installation or gameplay
test. Approximate full selected download sizes are:

For `hero`, `d3` and `hsb`, an independent CascLib reader also opened the
generated partial CASC storage and extracted the selected executable by CKey;
its bytes matched both the official content hash and the expanded loose file.

| Product | Selection | Encoded download | Installation difference |
| --- | --- | --- | --- |
| `s2` | Windows, enUS, US, speech + text | 24.8 GiB | `SC2Data`, switcher executable |
| `hero` | Windows, enUS, US, speech + text | 14.0 GiB | `HeroesData`, switcher executable |
| `d3` | Windows, enUS | 15.5 GiB | `Data`, 64-bit executable under `x64` |
| `hsb` | Windows, enUS, US, Production | 11.8 GiB | About 12 GiB of loose files must also be expanded |
| `wow_classic_era` | Windows, x86_64, enUS, US, speech + text | 4.5 GiB | Shared `Data` beside `_classic_era_/WowClassic.exe` |
| `s1` | Windows, x86_64, enUS, Release, noigr | 5.4 GiB | `Data`, executable under `x86_64` |
| `osi` | Windows, x86_64, enUS | 27.5 GiB | `Data`, `D2R.exe` and its shipped loader |

`s1` and `osi` bootstrap metadata and selected executables were also downloaded,
materialized and independently verified with CascLib. Game login and rendering
were not tested by these storage-reader checks.

WoW retail, both classic products, Overwatch and Diablo IV bootstrap manifests
also parsed successfully. WoW and Overwatch declare shared container layouts;
the backend supports shared CASC data with loose files under `installDirectory`.
A real Classic Era sample with 244 objects was independently opened from both
the container root and executable directory, with matching executable bytes.
The larger shared-container games still need complete validation. Platform and
content tags differ by product: Overwatch uses `TPWin`/`SPWin`, and Diablo IV puts
`base`, `speech` and `text` in the same tag type. Selecting only speech and text
would omit base content. These products are not advertised as ready to install.
The surveyed retail WoW selection needs about 123 GiB, classic about 76 GiB,
classic era about 4.5 GiB, and Overwatch about 74 GiB. Diablo IV's explicit Windows,
base + speech + text, lowres, enUS, US selection is about 89 GiB. These figures
exclude loose files, temporary space and optional content; they are manifest
estimates, not validated runnable installations.

Launch paths and data directories come from each pinned product configuration
under Blizzard's `tpr/configs/data` endpoint; tags and sizes come from its verified
install/download manifests. The [Keg implementation](https://github.com/MrMoonKr/keg-doc)
documents the same product configurations and tag-based installation model.

Consumers update this flake input with `nix flake update` to adopt committed game
records. Evaluation and normal installation never query `latest`. The usual
`flake.lock` pins Nix inputs; separate JSON lock directories are no longer used.
Old CDN objects may vanish, so preserve store outputs for long-term rollback.

## Immutable installation, mutable runtime

Game content is a read-only store output. Each launch creates a fresh private
writable projection using reflinks where available, with a full-copy fallback.
The projection is deleted when its Wine processes or native macOS client exit. Client writes
never modify the store output. Changes to installation files are discarded.
Selected user directories are linked to persistent state outside that projection.
For WoW products these default to `WTF`, `Interface` and `Screenshots` inside
the product subdirectory, retaining settings, addons and screenshots.
The native StarCraft runtime also retains `Maps/save` and `Maps/replays`.
These can be overridden with `games.PRODUCT.persistentDirectories`; an empty
list disables them. Blizzard documents its UI directories in
[Resetting the World of Warcraft User Interface](https://us.battle.net/support/en/article/7549).

Prefixes, settings, saves and account state persist under
`$XDG_STATE_HOME/battlenet/PRODUCT-BUILDKEY-CDNKEY-CONTENT/`, defaulting to
`~/.local/state/battlenet/`. Different versions get different prefixes; automatic
save or account migration is not implemented. Authentication and online service
availability remain external state handled by the official client.

Content derivations are unfree; overlay consumers retain their nixpkgs license
policy. The WC3 encoded closure is about 35.2 GB, with additional runtime space
needed. Snapshot identity checks do not certify CASC completeness.

## Implementation references

Format research used [blizzget](https://github.com/d07RiV/blizzget),
[CascLib](https://github.com/ladislav-zezula/CascLib),
[TACTLib](https://github.com/overtools/TACTLib),
[cascette](https://github.com/wowemulation-dev/cascette-rs), and
[SC2 NGDP documentation](https://github.com/sc2-arcade-watcher/sc2-file-format-docs).
