use ../update.nu push-catalogs

# Consume only ready indexes for the catalogs discovered by this run.
let paths = ($env.MATRIX | from json | get include | each {|job|
  if $job.catalog !~ '^[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)*$' {
    error make {msg: $"Invalid catalog name: ($job.catalog)"}
  }
  let manifest = ('incoming' | path join $"($job.catalog).json")
  if not ($manifest | path exists) {
    if $env.UPDATE_RESULT == 'success' { error make {msg: $"Missing ($manifest)"} }
    print --stderr $"No ready artifact for ($job.catalog)"
    return null
  }
  let index = (open $manifest)
  if $index.base != $env.GITHUB_SHA { error make {msg: $"Wrong revision in ($manifest)"} }
  if not $index.changed { return null }
  let name = $"($job.catalog).nix"
  let path = ('games' | path join $name)
  cp --force ('incoming' | path join $name) $path
  $path
})
push-catalogs $paths
