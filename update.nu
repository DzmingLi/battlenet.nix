# Release discovery and update orchestration. Binary formats stay in the backend.

const BACKEND = path self | path dirname | path join ngdp.py
const MAC_PRODUCTS = [w3 s2 s1 hero hsb d3 wow wow_classic wow_classic_era]

def exec [command: list<string>] {
  let result = (run-external ...$command | complete)
  if $result.exit_code != 0 { error make {msg: $result.stderr} }
  $result.stdout
}

def backend [...arguments: string] {
  let result = (^python3 -u $BACKEND ...$arguments | tee { print } | complete)
  if $result.exit_code != 0 { error make {msg: $result.stderr} }
}

export def cdn-url [url: string] {
  $url | str replace --regex '^https?://(?:(?:(?:us|eu|kr)\.)?cdn\.blizzard\.com|level3(?:\.ssl)?\.blizzard\.com|blizzard\.gcdn\.cloudn\.co\.kr)(/|$)' 'https://cdn.blizzard.com${1}'
}

def http-bytes [url: string] {
  let url = (cdn-url $url)
  mut last_error = ''
  for delay in [0sec 1sec 2sec 4sec] {
    if $delay != 0sec { sleep $delay }
    let response = (try {
      http get --raw --full --allow-errors --max-time 30sec $url
    } catch {|err| {status: 0, body: null, error: $err.msg} })
    if $response.status >= 200 and $response.status < 300 { return ($response.body | into binary) }
    $last_error = if $response.status == 0 { $response.error } else { $"HTTP ($response.status)" }
    if $response.status != 0 and $response.status not-in [408 429] and $response.status < 500 {
      error make {msg: $"($url): ($last_error)"}
    }
  }
  error make {msg: $"($url): ($last_error) after four attempts"}
}

def ngdp-table [url: string] {
  let rows = (http-bytes $url | decode utf-8 | lines | where {|line| $line != '' and not ($line | str starts-with '#') })
  let header = ($rows | first | split row '|' | each { split row '!' | first } | str join '|')
  [$header ...($rows | skip 1)] | str join (char newline) | from csv --separator '|' --no-infer
}

def select-row [rows: table, column: string, value: string] {
  let matches = ($rows | where {|row| ($row | get $column) == $value })
  if ($matches | length) != 1 { error make {msg: $"Expected one ($column)=($value)"} }
  $matches | first
}

def config-object [servers: list<string>, path: string, key: string] {
  if $key !~ '^[0-9a-f]{32}$' { error make {msg: 'Invalid configuration key'} }
  mut errors = []
  for server in $servers {
    let url = $"($server)/($path)/config/($key | str substring 0..1)/($key | str substring 2..3)/($key)"
    let result = (try {
      let bytes = (http-bytes $url)
      if ($bytes | hash md5) != $key { error make {msg: 'Configuration MD5 mismatch'} }
      {key: $key, url: $url, hash: $"sha256-($bytes | hash sha256 --binary | encode base64)"}
    } catch {|err| {error: $"($url): ($err.msg)"} })
    if ($result | get -o error) == null { return $result }
    $errors = ($errors | append $result.error)
  }
  error make {msg: $"No verified CDN configuration for ($key): ($errors | str join '; ')"}
}

export def resolve-release [product: string, region: string, endpoint: string, --previous: record = {}] {
  if $product !~ '^[a-z0-9_-]+$' { error make {msg: 'Invalid product code'} }
  let version = (select-row (ngdp-table $"($endpoint)/($product)/versions") Region $region)
  let cdn = (select-row (ngdp-table $"($endpoint)/($product)/cdns") Name $region)
  let advertised = ($cdn | get -o Servers | default '' | split row --regex '\s+' | where { $in != '' } | each { split row '?' | first | str trim --right --char '/' })
  let secure = ($advertised | where { $in | str starts-with 'https://' })
  let plain = ($advertised | where { $in | str starts-with 'http://' })
  let advertised_servers = if ($secure ++ $plain | is-empty) {
    $cdn.Hosts | split row --regex '\s+' | where { $in != '' } | each {|host| $"http://($host)" }
  } else { $secure ++ $plain }
  let servers = ($advertised_servers | each {|server| cdn-url $server } | uniq)
  let same_distribution = ($previous | get -o product) == $product and ($previous | get -o region) == $region
  let build = if $same_distribution and ($previous | get -o buildConfig.key) == $version.BuildConfig {
    $previous.buildConfig
  } else { config-object $servers $cdn.Path $version.BuildConfig }
  let cdn_config = if $same_distribution and ($previous | get -o cdnConfig.key) == $version.CDNConfig {
    $previous.cdnConfig
  } else { config-object $servers $cdn.Path $version.CDNConfig }
  {
    schemaVersion: 1, product: $product, region: $region,
    buildId: $version.BuildId, version: $version.VersionsName,
    buildConfig: $build,
    cdnConfig: $cdn_config,
    cdnPath: $cdn.Path, cdnServers: $servers,
    productConfig: ($version | get -o ProductConfig | default ''),
    keyRing: ($version | get -o KeyRing | default '')
  }
}

def product-configuration [release: record] {
  let key = $release.productConfig
  if $key !~ '^[0-9a-f]{32}$' { error make {msg: 'Missing verified product configuration'} }
  mut errors = []
  for server in $release.cdnServers {
    let result = (try {
      let url = $"($server)/tpr/configs/data/($key | str substring 0..1)/($key | str substring 2..3)/($key)"
      let bytes = (http-bytes $url)
      if ($bytes | hash md5) != $key { error make {msg: 'Product configuration MD5 mismatch'} }
      {data: ($bytes | decode utf-8 | from json), source: {key: $key, url: $url, hash: $"sha256-($bytes | hash sha256 --binary | encode base64)"}}
    } catch {|err| {error: $"($server): ($err.msg)"} })
    if ($result | get -o error) == null { return $result }
    $errors = ($errors | append $result.error)
  }
  error make {msg: $"No verified official product configuration: ($errors | str join '; ')"}
}

# China is a distinct distribution; zhCN alone also exists on global servers.
def language-defaults [original: record, locale: string] {
  let template = ($original | get -o installationDefaults | default ($original | get -o installation))
  if $template == null { error make {msg: 'Product needs installation defaults before preparing a variant'} }
  let locale_tags = ($template.storage.tags | where { $in =~ '^[a-z]{2}[A-Z]{2}$' })
  if ($locale_tags | is-empty) { error make {msg: 'Product has no verified language tag mapping'} }
  let storage = ($template.storage | select tags dataDirectory | update tags (
    $template.storage.tags | where { $in !~ '^[a-z]{2}[A-Z]{2}$' } | append $locale
  ))
  let settings = {storage: $storage, executable: $template.executable,
    args: ($template.args | each {|arg| if $arg in $locale_tags { $locale } else { $arg } })}
  if ($template.storage | get -o installDirectory) == null { $settings } else {
    $settings | upsert storage.installDirectory $template.storage.installDirectory
  }
}

def china-defaults [settings: record, configuration: record, locale: string] {
  let config = $configuration.data
  let allowed = ($config | get -o cn.config.display_locales | default (
    $config | get -o all.config.display_locales | default ($config | get -o all.config.supported_locales | default [])
  ))
  if $locale not-in $allowed { error make {msg: 'Language is not advertised for the China distribution'} }
  let extra = (($config | get -o all.config.extra_tags | default []) ++
    ($config | get -o platform.win.config.extra_tags | default []) ++
    ($config | get -o cn.config.extra_tags | default []))
  let tags = ($settings.storage.tags | each {|tag|
    if $tag in ['US' 'EU' 'KR' 'TW' 'CN'] { 'CN' } else { $tag }
  } | append $extra | uniq)
  let binary = ($config | get platform.win.config.binaries.game)
  let relative = ($binary | get -o relative_path_64 | default ($binary | get -o relative_path | default ''))
  if $relative == '' { error make {msg: 'China configuration has no Windows game executable'} }
  let folder = ($settings.storage | get -o installDirectory | default '')
  let executable = if $folder == '' { $relative } else { $"($folder)/($relative)" }
  $settings | update storage.tags $tags | update executable $executable |
    update args ($binary | get -o launch_arguments | default ($config | get -o all.config.launch_arguments | default []))
}

export def mac-defaults [original: record, configuration: record, locale: string] {
  let config = $configuration.data
  let mac = ($config | get -o platform.mac.config)
  if $mac == null { error make {msg: 'Product has no official native macOS configuration'} }
  let binary = ($mac | get binaries.game)
  let application = ($binary | get -o relative_path_64 | default $binary.relative_path)
  if not ($application | str ends-with '.app') { error make {msg: 'Native macOS application must be an app bundle'} }
  let template = ($original | get -o installationDefaults | default ($original | get -o installation | default {}))
  let old_storage = ($template | get -o storage | default {})
  let tags = ($old_storage | get -o tags | default [] | where {|tag|
    $tag not-in [Windows OSX x86_32 x86_64 arm64] and $tag !~ '^[a-z]{2}[A-Z]{2}$'
  } | prepend OSX | append $locale | append ($mac | get -o extra_tags | default []) | uniq)
  let storage = {tags: $tags, dataDirectory: ($mac | get -o data_dir | default (
    $config | get -o all.config.data_dir | default ($old_storage | get -o dataDirectory | default 'Data')
  ) | str trim --right --char '/')}
  let storage = if ($old_storage | get -o installDirectory) == null { $storage } else {
    $storage | upsert installDirectory $old_storage.installDirectory
  }
  {storage: $storage, application: $application,
   applicationPattern: (if ($binary | get -o switcher | default false) { $binary | get -o regex | default '' } else { '' }),
   executable: $"($application)/Contents/MacOS/($application | path basename | str replace --regex '\.app$' '')",
   args: ($binary | get -o launch_arguments | default []), runtime: 'native'}
}

export def to-nix [value: any] {
  let kind = ($value | describe)
  if $kind == 'nothing' { return 'null' }
  if $kind == 'string' { return ($value | to json --raw | str replace --all '${' '\${') }
  if $kind in ['bool' 'int'] { return ($value | to json --raw) }
  if ($kind | str starts-with 'record') {
    let fields = ($value | columns | sort | each {|key| $"(to-nix $key) = (to-nix ($value | get $key));" })
    return $"{ ($fields | str join ' ') }"
  }
  if ($kind | str starts-with 'list') or ($kind | str starts-with 'table') {
    return $"[ ($value | each {|item| to-nix $item } | str join (char newline)) ]"
  }
  error make {msg: $"Unsupported Nix data type: ($kind)"}
}

def write-nix [path: string, data: record] {
  let header = '# Generated by update.nu; do not edit content records by hand.'
  let installation = ($data | get -o installation)
  let expression = if $installation == null { to-nix $data } else {
    # Repeated CDN URLs can exceed GitHub's per-file limit on larger games.
    # Omit only URLs that can be reconstructed exactly from the pinned release.
    let prefix = $"($data.release.cdnServers | first)/($data.release.cdnPath)/data"
    let objects = ($installation.storage.objects | each {|object|
      let key = ($object.source | get -o archive | default $object.ekey)
      let expected = $"($prefix)/($key | str substring 0..1)/($key | str substring 2..3)/($key)"
      if $object.source.url == $expected {
        $object | update source ($object.source | reject url)
      } else { $object }
    })
    let compact = ($data | update installation.storage.objects $objects)
    [
      'let'
      $"  game = (to-nix $compact);"
      '  storage = game.installation.storage;'
      '  prefix = builtins.head game.release.cdnServers + "/" + game.release.cdnPath + "/data/";'
      '  restore = object: if object.source ? url then object else'
      '    let key = object.source.archive or object.ekey; in'
      '    object // { source = object.source // {'
      '      url = prefix + builtins.substring 0 2 key + "/" + builtins.substring 2 2 key + "/" + key;'
      '    }; };'
      'in game // { installation = game.installation // {'
      '  storage = storage // { objects = builtins.map restore storage.objects; };'
      '}; }'
    ] | str join (char newline)
  }
  let text = $"($header)\n($expression)\n"
  $text | save --force --raw $path
}

def write-verified [path: string, data: record] {
  write-nix $path $data
  let roundtrip = (exec ['nix' 'eval' '--json' '--file' $path] | from json)
  if $roundtrip != $data { error make {msg: 'Generated Nix data differs from verified records'} }
}

# Recover an interrupted first installation from its verified native checkpoint.
export def finish [checkpoint: string, --directory: string = 'games'] {
  let draft = (exec ['nix' 'eval' '--json' '--file' ($checkpoint | path expand) '--apply'
    'g: builtins.removeAttrs g ["installation"]'] | from json)
  let product = ($draft | get -o catalogProduct | default $draft.release.product)
  if $product !~ '^[a-z0-9_-]+$' { error make {msg: 'Invalid checkpoint product'} }
  let locale = ($draft | get -o locale | default '')
  if $locale != '' and $locale !~ '^[a-z]{2}[A-Z]{2}$' { error make {msg: 'Invalid checkpoint language'} }
  let country = ($draft | get -o serviceRegion | default '')
  if $country == 'cn' and $locale == '' { error make {msg: 'China checkpoint needs a language'} }
  if $country not-in ['' 'cn'] { error make {msg: 'Invalid checkpoint service region'} }
  let platform = ($draft | get -o platform | default 'windows')
  if $platform not-in ['windows' 'mac'] or ($platform == 'mac' and $country != '') { error make {msg: 'Invalid checkpoint platform'} }
  let name = if $platform == 'mac' {
    let stem = ($checkpoint | path parse | get stem)
    if $stem == $"($product).mac" { $stem } else { $"($product).mac.($locale)" }
  } else if $country == 'cn' { $"($product).cn.($locale)" } else if $locale == '' { $product } else { $"($product).($locale)" }
  let path = ($directory | path join $"($name).nix")
  let current = if ($path | path exists) {
    exec ['nix' 'eval' '--json' '--file' ($path | path expand)] | from json
  } else if $locale != '' { $draft } else { error make {msg: 'Missing product catalog'} }
  if ($current | get -o installation) != null { error make {msg: 'Product already initialized; refusing to overwrite it'} }
  let work = (exec ['mktemp' '-d'] | str trim)
  try {
    let storage_file = ($work | path join storage.json)
    exec ['nix' 'eval' '--json' '--file' ($checkpoint | path expand)
      '--apply' 'g: g.installation.storage'] | save --force --raw $storage_file
    backend validate-installation-lock $storage_file '--cache' ($work | path join cache)
    let generated = ($work | path join $"($name).nix")
    let raw = (open --raw $checkpoint)
    $"let checkpoint = \(($raw)\); in checkpoint // { installation = checkpoint.installation // { hash = null; }; }\n" | save --force --raw $generated
    mv --force $generated $path
    rm --recursive $work
  } catch {|err|
    rm --recursive --force $work
    error make {msg: $err.msg}
  }
}

# Check exported package names and base launcher profiles. Object derivation graphs
# belong to user builds; metadata updates validate each changed locale separately.
export def validate [] {
  let root = ($BACKEND | path dirname)
  let nixpkgs = (exec ['nix' 'eval' '--raw' '--impure' '--expr'
    $"\(builtins.getFlake (to-nix $root)\).inputs.nixpkgs.outPath"] | str trim)
  for system in [x86_64-linux aarch64-darwin] {
    let products = (exec ['nix' 'eval' '--json' $".#packages.($system)" '--apply' 'builtins.attrNames'] | from json)
    for product in $products {
      let filename = if $system == 'aarch64-darwin' { $"($product).mac.nix" } else { $"($product).nix" }
      validate-record ($root | path join games $filename) $nixpkgs
    }
  }
}

# Validate complete metadata and launcher selection without instantiating millions
# of object derivations. The backend has already checked the official closure.
def validate-record [path: string, nixpkgs: string] {
  let complete = (exec ['nix' 'eval' '--json' '--file' $path '--apply' 'g: g ? installation && (g.installation.storage.completeInstallation or false)'] | from json)
  if not $complete { return }
  let root = ($BACKEND | path dirname)
  let system = if $path =~ '\.mac(\.[a-z]{2}[A-Z]{2})?\.nix$' { 'aarch64-darwin' } else { 'x86_64-linux' }
  let expression = $"let pkgs = import (to-nix $nixpkgs) { system = (to-nix $system); config.allowUnfree = true; };
    api = pkgs.callPackage (to-nix ($root | path join default.nix)) {};
    game = import (to-nix $path);
    in assert game.installation.storage.completeInstallation;
    assert game.installation.storage.release == game.release;
    assert \(if (to-nix $system) == \"aarch64-darwin\" then builtins.elem \"OSX\" game.installation.storage.tags else true\);
    \(api.mkCatalogGame { inherit \(game\) release;
      content = pkgs.emptyDirectory;
      inherit \(game.installation\) executable args; }\).drvPath"
  print $"Validating ($path | path basename)"
  exec ['nix' 'eval' '--raw' '--impure' '--expr' $expression] | ignore
}

# Upload errors and release download propagation must remain visible. An existing
# immutable asset is accepted only after Nix verifies its unpacked NAR hash.
export def upload-catalog [archive: string, url: string, hash: string, repository: string] {
  mut last_error = ''
  for delay in [0sec 1sec 2sec 4sec] {
    if $delay != 0sec { sleep $delay }
    let upload = (^gh release upload catalogs $archive '--repo' $repository | complete)
    let fetched = (^nix eval --raw --impure --expr
      $"builtins.fetchTarball { url = (to-nix $url); sha256 = (to-nix $hash); }" | complete)
    if $fetched.exit_code == 0 { return ($fetched.stdout | str trim) }
    $last_error = $"Upload: ($upload.stderr)\nVerification: ($fetched.stderr)"
    print --stderr $last_error
  }
  error make {msg: $"Catalog publication failed after four attempts: ($last_error)"}
}

export def compact-catalogs [directory: string, --repository: string = 'DzmingLi/battlenet.nix'] {
  let release_tag = 'catalogs'
  let candidates = (glob ($directory | path join '*.nix') | where {|path|
    (ls $path | first | get size | into int) > 1048576
  })
  if ($candidates | is-empty) { return }
  let release = (^gh release view $release_tag '--repo' $repository | complete)
  if $release.exit_code != 0 {
    # A concurrent creator may win; view again before treating creation as failed.
    let created = (^gh release create $release_tag '--repo' $repository '--target' 'main'
      '--title' 'Pinned installation catalogs' '--notes'
      'Content-addressed native Nix installation metadata. Assets are immutable; source records pin unpacked NAR hashes.' | complete)
    if $created.exit_code != 0 { exec ['gh' 'release' 'view' $release_tag '--repo' $repository] | ignore }
  }
  for path in $candidates {
    let work = (exec ['mktemp' '-d'] | str trim)
    try {
      let payload = ($work | path join payload)
      mkdir $payload
      cp $path ($payload | path join catalog.nix)
      let semantic_hash = (exec ['nix' 'eval' '--json' '--file' ($path | path expand)] | hash sha256)
      let summary = (exec ['nix' 'eval' '--json' '--file' ($path | path expand) '--apply'
        'g: g // { installation = g.installation // { storage = builtins.removeAttrs g.installation.storage ["bootstrap" "objects" "installFiles"]; }; }'] | from json)
      if not ($summary | get -o installation.storage.completeInstallation | default false) { error make {msg: 'Cannot compact an incomplete installation'} }
      let hash = (exec ['nix' 'hash' 'path' $payload] | str trim)
      let tar = ($work | path join catalog.tar)
      exec ['tar' '--sort=name' '--mtime=@1' '--owner=0' '--group=0' '--numeric-owner'
        '-cf' $tar '-C' $work 'payload'] | ignore
      exec ['gzip' '-1' '--no-name' $tar] | ignore
      let compressed = ($work | path join catalog.tar.gz)
      let digest = (open --raw $compressed | into binary | hash sha256)
      let asset = $"($digest).tar.gz"
      let archive = ($work | path join $asset)
      mv $compressed $archive
      let url = $"https://github.com/($repository)/releases/download/($release_tag)/($asset)"
      let fetched = (upload-catalog $archive $url $hash $repository)
      let roundtrip = (exec ['nix' 'eval' '--json' '--file' ($fetched | path join catalog.nix)] | hash sha256)
      if $roundtrip != $semantic_hash { error make {msg: 'Published catalog differs from the verified installation data'} }
      let text = [
        '# Generated by update.nu; full object data is content-addressed outside Git.'
        'import ../load-catalog.nix {'
        $"  url = (to-nix $url);"
        $"  hash = (to-nix $hash);"
        $"  game = (to-nix $summary);"
        '}'
      ] | str join (char newline)
      $text | save --force --raw $path
      rm --recursive $work
      print $"Compacted ($path | path basename) → ($asset)"
    } catch {|err|
      rm --recursive --force $work
      error make {msg: $err.msg}
    }
  }
}

# Publish in an isolated checkout, preserving edits in the caller's workspace.
# A catalog changed since this job's checkout belongs to another publisher;
# leave it intact and let the next discovery decide whether another update is due.
export def push-catalogs [paths: list<string>] {
  let base = (exec ['git' 'rev-parse' 'HEAD'] | str trim)
  let source = (pwd)
  for attempt in 1..8 {
    exec ['git' 'fetch' 'origin' 'main'] | ignore
    let work = (exec ['mktemp' '-d'] | str trim)
    exec ['git' 'worktree' 'add' '--detach' $work 'FETCH_HEAD'] | ignore
    try {
      for path in $paths {
        let changed = (^git -C $work diff --quiet $base HEAD -- $path | complete)
        if $changed.exit_code == 1 {
          print $"Skipping concurrently updated catalog ($path); remote version retained"
        } else if $changed.exit_code != 0 { error make {msg: $changed.stderr} } else {
          mkdir ($work | path join $path | path dirname)
          cp --force ($source | path join $path) ($work | path join $path)
          exec ['git' '-C' $work 'add' '--' $path] | ignore
        }
      }
      let diff = (^git -C $work diff --cached --quiet | complete)
      if $diff.exit_code == 0 {
        exec ['git' 'worktree' 'remove' '--force' $work] | ignore
        print 'Remote catalogs already current; no commit needed'
        return
      }
      if $diff.exit_code != 1 { error make {msg: $diff.stderr} }
      exec ['git' '-C' $work '-c' 'user.name=github-actions[bot]'
        '-c' 'user.email=41898282+github-actions[bot]@users.noreply.github.com'
        'commit' '-m' 'Update pinned Battle.net game catalog'] | print
      let pushed = (^git -C $work push origin HEAD:main | complete)
      exec ['git' 'worktree' 'remove' '--force' $work] | ignore
      if $pushed.exit_code == 0 { print $pushed.stderr; return }
      if $pushed.stderr !~ '(fetch first|non-fast-forward|failed to update ref|cannot lock ref)' {
        error make {msg: $pushed.stderr}
      }
      print $"Concurrent push; retrying publication \(($attempt)/8\)"
    } catch {|err|
      ^git worktree remove --force $work | complete | ignore
      error make {msg: $err.msg}
    }
  }
  error make {msg: 'Publication still contended after eight attempts; verified catalogs are saved as artifacts'}
}

export def publish [directory: string] {
  compact-catalogs $directory --repository ($env | get -o GITHUB_REPOSITORY | default 'DzmingLi/battlenet.nix')
  exec ['git' 'add' '--' $directory] | ignore
  let diff = (^git diff --cached --quiet -- $directory | complete)
  if $diff.exit_code == 0 { print 'Catalog unchanged; no commit needed'; return }
  if $diff.exit_code != 1 { error make {msg: $diff.stderr} }
  let root = ($BACKEND | path dirname)
  let nixpkgs = (exec ['nix' 'eval' '--raw' '--impure' '--expr' $"\(builtins.getFlake (to-nix $root)\).inputs.nixpkgs.outPath"] | str trim)
  let paths = (exec ['git' 'diff' '--cached' '--name-only' '--diff-filter=ACMR' '--' $directory] | lines | where { $in | str ends-with '.nix' })
  for path in $paths { validate-record ($path | path expand) $nixpkgs }
  push-catalogs $paths
}

# Only digest-named metadata assets in the catalogs release are eligible.
export def catalog-prune-plan [assets: list, keep: list<string>, cutoff: datetime] {
  $assets | where {|asset|
    ($asset.name =~ '^[0-9a-f]{64}\.tar\.gz$' and
     $asset.state == 'uploaded' and $asset.name not-in $keep and
     ($asset.created_at | into datetime) < $cutoff)
  }
}

export def catalog-retained-runs [runs: list, cutoff: datetime] {
  # GitHub re-runs keep their original SHA and are allowed for 30 days.
  # Include this run too: cleanup must preserve its own retry dependencies.
  $runs | where {|run|
    $run.status != 'completed' or ($run.created_at | into datetime) >= $cutoff
  } | select id head_sha created_at | sort-by id
}

def retained-catalog-runs [repository: string, cutoff: datetime] {
  exec ['gh' 'api' '--paginate' '--slurp' $"repos/($repository)/actions/runs?per_page=100"] |
    from json | get workflow_runs | flatten |
    catalog-retained-runs $in $cutoff
}

def catalog-references [revision: string] {
  let result = (^git grep -h -o -E 'https://github\.com/[^" ]+/releases/download/catalogs/[0-9a-f]{64}\.tar\.gz' $revision -- games | complete)
  if $result.exit_code == 1 { return [] }
  if $result.exit_code != 0 { error make {msg: $result.stderr} }
  $result.stdout | lines | each {|url| $url | split row '/' | last } | uniq
}

export def prune-catalogs [--repository: string = 'DzmingLi/battlenet.nix', --apply] {
  let retention_cutoff = (date now) - 30day
  let runs = (retained-catalog-runs $repository $retention_cutoff)
  exec ['git' 'fetch' '--no-tags' 'origin' 'main'] | ignore
  let main = (exec ['git' 'rev-parse' 'FETCH_HEAD'] | str trim)
  mut keep = (catalog-references $main)
  if ($keep | is-empty) { error make {msg: 'Main has no pinned catalog assets; refusing release cleanup'} }
  try {
    for revision in ($runs | get head_sha | uniq) {
      let local = (^git cat-file -e $"($revision)^{tree}" | complete)
      if $local.exit_code != 0 { exec ['git' 'fetch' '--refetch' '--no-tags' 'origin' $revision] | ignore }
      $keep = ($keep | append (catalog-references $revision) | uniq)
    }
  } catch {|err|
    print --stderr $"Cannot inspect retry dependencies; release cleanup deferred: ($err.msg)"
    return
  }
  # Keep recent uncommitted uploads for retry/recovery, including this run's.
  # An older unfinished run also protects uploads since it started.
  let oldest = if ($runs | is-empty) { $retention_cutoff } else {
    $runs | sort-by created_at | first | get created_at | into datetime
  }
  let cutoff = if $oldest < $retention_cutoff { $oldest } else { $retention_cutoff }
  let release = (exec ['gh' 'api' $"repos/($repository)/releases/tags/catalogs"] | from json)
  let assets = (exec ['gh' 'api' '--paginate' '--slurp'
    $"repos/($repository)/releases/($release.id)/assets?per_page=100"] | from json | flatten)
  let candidates = (catalog-prune-plan $assets $keep $cutoff)
  let bytes = ($candidates | get size | append 0 | math sum)
  print $"Catalog assets: ($assets | length); protected references: ($keep | length); obsolete: ($candidates | length), ($bytes) bytes"
  for asset in $candidates {
    if not $apply { print $"Would delete ($asset.name)"; continue }
    # Abort if publishers advance main or the retained-run set changes during cleanup.
    let latest = (exec ['gh' 'api' $"repos/($repository)/commits/main" '--jq' '.sha'] | str trim)
    if $latest != $main or (retained-catalog-runs $repository $retention_cutoff) != $runs {
      print 'Publication state changed; remaining cleanup deferred to the next run'
      return
    }
    exec ['gh' 'api' '--method' 'DELETE' $"repos/($repository)/releases/assets/($asset.id)"] | ignore
    print $"Deleted ($asset.name)"
  }
}

# Prior object hashes are only an optimization. Missing historical release assets
# must not prevent reconstructing and verifying the current official closure.
export def reuse-catalog [path: string, output: string] {
  let result = (^nix eval --json --file ($path | path expand)
    --apply 'g: { objects = g.installation.storage.objects or []; }' | complete)
  if $result.exit_code != 0 {
    print --stderr $"Cannot reuse ($path); downloading and verifying objects instead: ($result.stderr)"
    return []
  }
  # Parse before writing so a broken response never becomes a reuse lock.
  $result.stdout | from json | to json --raw | save --force --raw $output
  ['--reuse-lock' $output]
}

# Discover language work from verified official configurations. Each result is
# independently generated and published by CI, so one failure cannot block others.
export def language-jobs [--directory: string = 'games', --product: string, --endpoint: string = '', --platform: string = ''] {
  if $platform not-in ['' 'windows' 'mac'] { error make {msg: 'Invalid discovery platform'} }
  let selected_platform = $platform
  mut jobs = []
  let sources = (glob ($directory | path join '*.nix') | sort | where {|path|
    let stem = ($path | path parse | get stem)
    ($stem =~ '^[a-z0-9_-]+(\.mac|\.cn\.[a-z]{2}[A-Z]{2})?$' and
      ($product == null or $stem == $product or $stem == $"($product).mac" or ($stem | str starts-with $"($product).cn.")))
  })
  for path in $sources {
    let discovered = (try {
      mut product_jobs = []
      let original = (exec ['nix' 'eval' '--json' '--file' $path '--apply'
        'g: (builtins.removeAttrs g ["installation"]) // (if g ? installation then { installation = { hash = g.installation.hash or null; complete = g.installation.storage.completeInstallation or false; storage.tags = g.installation.storage.tags; }; } else {})'] | from json)
      let country = ($original | get -o serviceRegion | default '')
      let logical = ($original | get -o catalogProduct | default $original.release.product)
      let platform = ($original | get -o platform | default 'windows')
      let base_catalog = if $platform == 'mac' { $"($logical).mac" } else { $logical }
      let service = if $endpoint != '' { $endpoint } else if $country == 'cn' {
        'http://cn.patch.battlenet.com.cn:1119'
      } else { 'http://us.patch.battle.net:1119' }
      let release = (resolve-release $original.release.product $original.release.region $service --previous $original.release)
      let complete = ($original | get -o installation.complete | default false)
      let configuration = if $complete or ($country == '' and $logical in $MAC_PRODUCTS) { product-configuration $release } else { null }
      let supported = if not $complete { [] } else if $country == 'cn' {
        $configuration.data | get -o cn.config.display_locales | default (
          $configuration.data | get -o all.config.display_locales | default $configuration.data.all.config.supported_locales
        ) | where {|locale| $locale in $configuration.data.all.config.supported_locales }
      } else { $configuration.data.all.config.supported_locales }
      let bundled = ($original | get -o installation.storage.tags | default [] | where { $in =~ '^[a-z]{2}[A-Z]{2}$' })
      let targets = if $country == 'cn' {
        $supported | each {|locale| {catalog: $"($logical).cn.($locale)", locale: $locale} }
      } else {
        [{catalog: $base_catalog, locale: ''}] ++ ($supported | where {|locale| $locale not-in $bundled } |
          each {|locale| {catalog: $"($base_catalog).($locale)", locale: $locale} })
      }
      if ($country == '' and $platform == 'windows' and $logical in $MAC_PRODUCTS and
          ($configuration | get -o data.platform.mac.config.binaries.game.relative_path) != null and
          not ($directory | path join $"($logical).mac.nix" | path exists)) {
        $product_jobs = ($product_jobs | append {product: $logical, locale: '', region: '',
          platform: 'mac', catalog: $"($logical).mac", initialize: true})
      }
      for target in $targets {
        if $target.locale != '' and $target.locale !~ '^[a-z]{2}[A-Z]{2}$' {
          error make {msg: 'Official configuration has an invalid locale'}
        }
        let destination = ($directory | path join $"($target.catalog).nix")
        let missing = not ($destination | path exists)
        let old = if $missing { null } else { exec ['nix' 'eval' '--json' '--file' $destination '--apply' 'g: { inherit (g) release; }'] | from json }
        let changed = ($missing or ($old.release | select buildConfig cdnConfig version productConfig keyRing) !=
          ($release | select buildConfig cdnConfig version productConfig keyRing))
        if $changed {
          $product_jobs = ($product_jobs | append {product: $logical, locale: $target.locale, region: $country,
            platform: $platform, catalog: $target.catalog, initialize: $missing})
        }
      }
      $product_jobs
    } catch {|err|
      let stem = ($path | path parse | get stem)
      let parts = ($stem | split row '.')
      let country = if 'cn' in $parts { 'cn' } else { '' }
      print --stderr $"Discovery failed for ($stem): ($err.msg)"
      [{product: ($parts | first), locale: '', region: $country,
        platform: (if 'mac' in $parts { 'mac' } else { 'windows' }), catalog: $stem, initialize: false, discovery_error: $err.msg}]
    })
    $jobs = ($jobs ++ $discovered)
  }
  $jobs | uniq | where {|job| $selected_platform == '' or ($job | get -o platform | default 'windows') == $selected_platform }
}

# A failed invocation prevents publishing its staged catalog updates.
def main [
  --directory: string = 'games'
  --product: string
  --catalog: string = '' # Update only one existing catalog; used by language CI.
  --check
  --endpoint: string = ''
  --workers: int = 8
  --initialize # Prepare the first complete installation for the selected product.
  --locale: string = '' # Initialize a separately verified language variant.
  --region: string = '' # Initialize a China distribution with cn; language defaults to zhCN.
  --platform: string = '' # Initialize native macOS content with mac; defaults to Windows.
  --tags: string = '' # Override the product's default manifest tags.
  --executable: string = '' # Override the product's default Windows executable.
  --data-directory: string = '' # Override the product's default CASC directory.
  --game-args: string = '' # Override launch arguments with a JSON string array.
  --commit # Validate and commit directly to main; intended for GitHub Actions.
  --checkpoint-directory: string = '' # Preserve verified resources before installation reconstruction.
] {
  if $workers < 1 or $workers > 16 { error make {msg: 'Workers must be between 1 and 16'} }
  if $check and $commit { error make {msg: '--check cannot be combined with --commit'} }
  if $catalog != '' and ($initialize or $catalog !~ '^[a-z0-9_-]+(\.mac(\.[a-z]{2}[A-Z]{2})?|\.(cn\.)?[a-z]{2}[A-Z]{2})?$') {
    error make {msg: '--catalog requires a valid existing catalog name and cannot initialize'}
  }
  if $initialize and $product == null {
    error make {msg: '--initialize requires --product'}
  }
  if $platform not-in ['' 'mac'] or ($platform != '' and (not $initialize or $region != '')) {
    error make {msg: '--platform accepts mac during international initialization'}
  }
  if $locale != '' and (not $initialize or $product == null or $locale !~ '^[a-z]{2}[A-Z]{2}$') {
    error make {msg: '--locale requires --initialize, --product and a locale such as zhCN'}
  }
  if $region not-in ['' 'cn'] or ($region != '' and not $initialize) {
    error make {msg: '--region accepts cn and requires --initialize with --product'}
  }
  let selected_locale = if $region == 'cn' and $locale == '' { 'zhCN' } else if $platform == 'mac' and $locale == '' { 'enUS' } else { $locale }
  let selected_tags = ($tags | split row --regex '\s+' | where { $in != '' })
  let launch_args = (if $game_args == '' { [] } else { $game_args | from json })
  if not (($launch_args | describe) | str starts-with 'list') {
    error make {msg: '--game-args must be a JSON array'}
  }
  if ($launch_args | any {|argument| ($argument | describe) != 'string' }) {
    error make {msg: '--game-args must contain only strings'}
  }
  let files = (glob ($directory | path join '*.nix') | sort | where {|path|
    let stem = ($path | path parse | get stem)
    (($catalog == '' or $stem == $catalog) and
      ($product == null or $stem == $product or (not $initialize and ($stem | str starts-with $"($product)."))))
  })
  if ($files | is-empty) { error make {msg: 'No matching catalog products'} }
  let work = (exec ['mktemp' '-d'] | str trim)
  try {
    mut staged = []
    for path in $files {
      let original = (exec ['nix' 'eval' '--json' '--file' $path '--apply'
        'g: g // (if g ? installation then { installation = g.installation // { storage = builtins.removeAttrs g.installation.storage ["objects" "bootstrap" "installFiles"]; }; } else {})'] | from json)
      let target_platform = if $platform == 'mac' { 'mac' } else { $original | get -o platform | default 'windows' }
      let variant = $initialize and $selected_locale != ''
      let destination = if $platform == 'mac' {
        $directory | path join (if $locale == '' { $"($product).mac.nix" } else { $"($product).mac.($locale).nix" })
      } else if $region == 'cn' {
        $directory | path join $"($product).cn.($selected_locale).nix"
      } else if $variant { $directory | path join $"($product).($selected_locale).nix" } else { $path }
      if $variant and ($destination | path exists) { error make {msg: 'Variant already exists; use a normal update'} }
      let name = ($destination | path parse | get stem)
      let country = if $region == 'cn' { 'cn' } else { $original | get -o serviceRegion | default '' }
      let raw_product = if $region == 'cn' {
        match $product { 'd3' => 'd3cn', 'osi' => 'osic', _ => $original.release.product }
      } else { $original.release.product }
      let download_region = if $country == 'cn' { 'cn' } else { $original.release.region }
      let service = if $endpoint != '' { $endpoint } else if $country == 'cn' {
        'http://cn.patch.battlenet.com.cn:1119'
      } else { 'http://us.patch.battle.net:1119' }
      let release = (resolve-release $raw_product $download_region ($service | str trim --right --char '/') --previous $original.release)
      let configuration = if $variant or $country == 'cn' or $target_platform == 'mac' { product-configuration $release } else { null }
      let language = if $variant { $selected_locale } else { $original | get -o locale | default '' }
      if $language != '' and $configuration != null and $language not-in $configuration.data.all.config.supported_locales {
        error make {msg: 'Language is not supported by the verified official product configuration'}
      }
      let current = if $platform == 'mac' {
        $original | reject -o installation locales chinaLocales | upsert platform 'mac' | upsert locale $selected_locale |
          upsert productConfiguration $configuration.source | upsert installationDefaults (mac-defaults $original $configuration $selected_locale)
      } else if $variant {
        # Metadata-only products can be researched with explicit manifest tags;
        # China still obtains the executable and arguments from verified config.
        let template = if ($original | get -o installationDefaults | default ($original | get -o installation)) == null and $tags != '' {
          $original | upsert installationDefaults {
            storage: {tags: $selected_tags, dataDirectory: (if $data_directory == '' { 'Data' } else { $data_directory })},
            executable: $executable, args: $launch_args
          }
        } else { $original }
        $original | reject -o installation | upsert locale $selected_locale | upsert productConfiguration $configuration.source |
          upsert installationDefaults (language-defaults $template $selected_locale)
      } else { $original }
      let current = if $country == 'cn' {
        $current | upsert catalogProduct ($original | get -o catalogProduct | default $product) |
          upsert serviceRegion 'cn' | upsert productConfiguration $configuration.source |
          upsert installationDefaults (china-defaults $current.installationDefaults $configuration $language)
      } else { $current }
      let old = $original.release
      let existing = ($current | get -o installation)
      if $initialize and $existing != null { error make {msg: 'Product already has a complete installation; use a normal update'} }
      if not $initialize and ($release | select buildConfig cdnConfig version | upsert productConfig ($release | get -o productConfig) | upsert keyRing ($release | get -o keyRing)) == ($old | select buildConfig cdnConfig version | upsert productConfig ($old | get -o productConfig) | upsert keyRing ($old | get -o keyRing)) {
        print $"($name): unchanged \(($old.version)\)"
        continue
      }
      print $"($name): ($old.version) → ($release.version); initialize=($initialize)"
      if $check { continue }
      mut updated = ($current | update release $release)
      if $existing != null or $initialize {
        let defaults = if $target_platform == 'mac' {
          mac-defaults $current $configuration (if $language == '' { 'enUS' } else { $language })
        } else { $current | get -o installationDefaults | default {} }
        let settings = if $existing != null and $country != 'cn' and $target_platform != 'mac' { $existing } else {
          let default_storage = ($defaults | get -o storage | default {})
          let chosen_tags = if $tags != '' { $selected_tags } else { $default_storage | get -o tags | default [] }
          let chosen_executable = if $executable != '' { $executable } else { $defaults | get -o executable | default '' }
          if ($chosen_tags | is-empty) or $chosen_executable == '' {
            error make {msg: 'Product needs verified installation defaults, or explicit --tags and --executable'}
          }
          if $language != '' and $language not-in $chosen_tags { error make {msg: 'Installation tags must contain the requested locale'} }
          if $country == 'cn' and ($default_storage.tags | any {|tag| $tag not-in $chosen_tags }) {
            error make {msg: 'China installation overrides must retain verified distribution tags'}
          }
          {storage: {
             tags: $chosen_tags,
             dataDirectory: (if $data_directory != '' { $data_directory } else { $default_storage | get -o dataDirectory | default 'Data' })
           },
           executable: $chosen_executable,
           args: (if $game_args != '' { $launch_args } else { $defaults | get -o args | default [] })}
          | update storage {|record|
              let directory = ($default_storage | get -o installDirectory | default '')
              if $directory == '' { $record.storage } else { $record.storage | upsert installDirectory $directory }
            }
        }
        let cache = ($work | path join cache)
        mkdir $cache
        let release_file = ($work | path join release.json)
        let bootstrap_file = ($work | path join bootstrap.json)
        let storage_file = ($work | path join storage.json)
        let plan_file = ($work | path join plan.json)
        $release | to json --raw | save --force --raw $release_file
        backend prefetch-manifests $release_file '--cache' $cache '--output' $bootstrap_file
        let settings = if $target_platform == 'mac' {
          let available = (exec ['python3' $BACKEND 'inspect-manifests' $bootstrap_file '--cache' $cache] | from json)
          let install_tags = ($available.install.tags | get name)
          let download_tags = ($available.download.tags | get name)
          let region_tags = ($available.install.tags | where type == 4 | get name)
          let distribution = ($release.region | str uppercase)
          let region_selection = if ($region_tags | any {|tag| $tag in $settings.storage.tags }) {
            []
          } else if $distribution in $region_tags and $distribution in $download_tags { [$distribution] } else { [] }
          $settings | update storage.tags ($settings.storage.tags | append $region_selection | append (
            if 'arm64' in $install_tags and 'arm64' in $download_tags { [arm64] } else { [] }
          ) | uniq) | upsert application $defaults.application | upsert applicationPattern $defaults.applicationPattern | upsert runtime 'native'
        } else { $settings }
        backend plan-install $bootstrap_file '--cache' $cache '--tags' ...$settings.storage.tags '--output' $plan_file
        backend prefetch-indexes $bootstrap_file '--cache' $cache '--output' $bootstrap_file
        let install_directory = ($settings.storage | get -o installDirectory | default '')
        let settings = if $target_platform == 'mac' {
          let native_file = ($work | path join native.json)
          (backend resolve-macos-launch $bootstrap_file '--cache' $cache '--tags' ...$settings.storage.tags
            '--application' $settings.application '--pattern' $settings.applicationPattern '--output' $native_file)
          let native = (open $native_file)
          $settings | merge $native | update executable (if $install_directory == '' { $native.executable } else { $"($install_directory)/($native.executable)" })
        } else { $settings }
        let prefix = $"($install_directory)/"
        let manifest_executable = if $install_directory == '' { $settings.executable } else {
          if not ($settings.executable | str starts-with $prefix) {
            error make {msg: 'Executable must be inside the configured installDirectory'}
          }
          $settings.executable | str replace $prefix ''
        }
        # Windows product configs sometimes spell WoWClassic/WowClassic differently.
        # Resolve one selected manifest entry and retain its exact on-disk spelling.
        let matches = (open $plan_file | get installFiles | where {|file|
          ($file.path | str lowercase) == ($manifest_executable | str lowercase)
        })
        if ($matches | length) != 1 {
          error make {msg: 'Selected executable is absent or ambiguous in the official installation manifest'}
        }
        let actual = ($matches | first | get path)
        let settings = ($settings | update executable (if $install_directory == '' { $actual } else { $"($install_directory)/($actual)" }))
        let layout_args = if $install_directory == '' { [] } else { ['--install-directory' $install_directory] }
        # Reuse the selected previous catalog and both platform base catalogs.
        # These records are fetched through their pinned NAR hashes.
        mut reuse_args = []
        let prior = ($original | get -o installation.storage)
        if $prior != null {
          let prior_file = ($work | path join prior.json)
          $reuse_args = ($reuse_args | append (reuse-catalog $path $prior_file))
        }
        for stem in [$raw_product $"($raw_product).mac"] {
          let base = ($directory | path join $"($stem).nix")
          if ($base | path exists) and ($base | path expand) != ($path | path expand) {
            let reuse_file = ($work | path join $"reuse-($stem).json")
            $reuse_args = ($reuse_args | append (reuse-catalog $base $reuse_file))
          }
        }
        let cache_flags = ['--discard-cache' ...$reuse_args]
        backend prepare-installation $bootstrap_file '--cache' $cache '--tags' ...$settings.storage.tags '--workers' ($workers | into string) '--data-directory' $settings.storage.dataDirectory ...$cache_flags ...$layout_args '--output' $storage_file
        $updated = ($updated | upsert installation ($settings | update storage (open $storage_file) | upsert hash null))
        if $checkpoint_directory != '' {
          mkdir $checkpoint_directory
          write-verified ($checkpoint_directory | path join $"($name).nix") $updated
        }
        # Completeness is validated against pinned manifests by the backend.
        # Users assemble the individually hashed inputs in a pure derivation.
        rm --recursive $cache
      }
      let generated = ($work | path join ($path | path basename))
      write-verified $generated $updated
      $staged = ($staged | append {source: $generated, destination: $destination})
    }
    for entry in $staged { mv --force $entry.source $entry.destination }
    rm --recursive $work
  } catch {|err|
    rm --recursive --force $work
    error make {msg: $err.msg}
  }
  if $commit { publish $directory }
}
