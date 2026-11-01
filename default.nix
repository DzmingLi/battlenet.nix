{ lib, python3, writeShellApplication, writeText, runCommand, fetchurl, cacert, util-linux, wineWow64Packages, callPackage }:
let
  darwin = lib.hasSuffix "-darwin" python3.system;
  runtime = if darwin then {} else callPackage ./shared-runtime.nix {};
  # Only transport URLs change: existing catalogs and installation hashes stay
  # intact, including .build.info. China keeps its official NetEase hosts.
  cdnUrl = url:
    let match = builtins.match "https?://(((us|eu|kr)[.])?cdn[.]blizzard[.]com|level3([.]ssl)?[.]blizzard[.]com|blizzard[.]gcdn[.]cloudn[.]co[.]kr)(/.*)?" url;
    in if match == null then url else
      "https://cdn.blizzard.com" + (if builtins.elemAt match 4 == null then "" else builtins.elemAt match 4);
  fetchCdn = descriptor: fetchurl {
    urls = lib.unique [ (cdnUrl descriptor.url) descriptor.url ];
    inherit (descriptor) hash;
  };
  source = lib.cleanSourceWith {
    src = ./.;
    filter = path: type: lib.cleanSourceFilter path type
      && (type == "directory" || lib.hasSuffix ".py" path);
  };
  readLock = lockFile:
    let lock = builtins.fromJSON (builtins.readFile lockFile);
    in assert lock.schemaVersion == 1;
       assert builtins.match "[a-z0-9_-]+" lock.product != null;
       assert builtins.match "[0-9a-f]{32}" lock.buildConfig.key != null;
       assert builtins.match "[0-9a-f]{32}" lock.cdnConfig.key != null;
       lock;
  mkMetadata = { lockFile }:
    let lock = readLock lockFile;
        build = fetchCdn lock.buildConfig;
        cdn = fetchCdn lock.cdnConfig;
    in runCommand "battlenet-${lock.product}-${lock.buildId}-metadata" {} ''
      mkdir -p "$out"
      cp ${lockFile} "$out/release.json"
      cp ${build} "$out/build.config"
      cp ${cdn} "$out/cdn.config"
    '';
  mkManifests = { manifestLockFile }:
    let
      lock = builtins.fromJSON (builtins.readFile manifestLockFile);
      objects = lib.genAttrs [ "encoding" "install" "download" ]
        (name: fetchCdn lock.objects.${name});
    in assert lock.schemaVersion == 1;
    runCommand "battlenet-${lock.release.product}-${lock.release.buildId}-manifests" {
      nativeBuildInputs = [ python3 ];
    } (''
      mkdir -p "$out"
      cp ${manifestLockFile} "$out/bootstrap-lock.json"
    '' + lib.concatMapStrings (name: ''
      cp ${objects.${name}} "$out/${name}.blte"
    '') [ "encoding" "install" "download" ] + ''
      python3 ${source}/ngdp.py verify-manifests "$out" ${manifestLockFile}
    '');
  mkPlan = { manifestLockFile, tags }:
    let
      lock = builtins.fromJSON (builtins.readFile manifestLockFile);
      manifests = mkManifests { inherit manifestLockFile; };
      indexes = map (index: {
        inherit (index) archive;
        source = fetchCdn index;
      }) lock.archives;
    in runCommand "battlenet-${lock.release.product}-${lock.release.buildId}-plan.json" {
      nativeBuildInputs = [ python3 ];
    } (''
      mkdir -p "$TMPDIR/cache"
    '' + lib.concatMapStrings (name: ''
      cp ${manifests}/${name}.blte "$TMPDIR/cache/${lock.objects.${name}.ekey}"
    '') [ "encoding" "install" "download" ] + lib.concatMapStrings (index: ''
      cp ${index.source} "$TMPDIR/cache/${index.archive}.index"
    '') indexes + ''
      python3 ${source}/ngdp.py resolve-plan ${manifestLockFile} \
        --cache "$TMPDIR/cache" --tags ${lib.escapeShellArgs tags} --output "$TMPDIR/plan.json"
      cp "$TMPDIR/plan.json" "$out"
    '');
  mkFile = { fileLockFile }:
    let file = builtins.fromJSON (builtins.readFile fileLockFile);
    in assert file.schemaVersion == 1;
    runCommand "battlenet-${file.release.product}-${file.ekey}-file" {
      nativeBuildInputs = [ python3 ];
      outputHashMode = "flat";
      outputHashAlgo = "sha256";
      outputHash = file.decodedHash;
      SSL_CERT_FILE = "${cacert}/etc/ssl/certs/ca-bundle.crt";
      impureEnvVars = [ "http_proxy" "https_proxy" "all_proxy" "no_proxy" "HTTP_PROXY" "HTTPS_PROXY" "ALL_PROXY" "NO_PROXY" ];
      meta.license = lib.licenses.unfree;
    } ''
      python3 ${source}/ngdp.py materialize-file ${fileLockFile} --output "$TMPDIR/decoded-file"
      cp "$TMPDIR/decoded-file" "$out"
    '';
  mkEncodedObject = { descriptor }:
    # Hundreds of thousands of objects: avoid a full stdenv for each fetch.
    # Fixed-output identity depends on name and hash, not the build's CDN URL.
    builtins.derivation {
      name = "battlenet-${descriptor.ekey}-encoded";
      system = python3.system;
      builder = "${python3}/bin/python3";
      args = [ "-c" ''
        import json, os, sys
        sys.path.insert(0, os.environ["backend"])
        import storage, tact
        tact.atomic_bytes(os.environ["out"], storage.fetch_locked(json.loads(os.environ["descriptor"])))
      '' ];
      backend = source;
      descriptor = builtins.toJSON descriptor;
      outputHashMode = "flat";
      outputHashAlgo = "sha256";
      outputHash = descriptor.hash;
      SSL_CERT_FILE = "${cacert}/etc/ssl/certs/ca-bundle.crt";
      impureEnvVars = [ "http_proxy" "https_proxy" "all_proxy" "no_proxy" "HTTP_PROXY" "HTTPS_PROXY" "ALL_PROXY" "NO_PROXY" ];
    };
  # Object output identities contain no build/product: unchanged bytes are shared.
  mkObjectCache = lock:
    let
      cache = runCommand "battlenet-${lock.release.product}-locked-storage-cache" {} (
        "mkdir -p \"$out\"\n" + lib.concatMapStrings (object: ''
          ln -s ${mkEncodedObject { descriptor = object; }} "$out/${object.ekey}"
        '') lock.objects + lib.concatMapStrings (name:
          let config = lock.release.${name};
              file = fetchCdn config;
          in ''
            ln -s ${file} "$out/${config.key}.config"
          '') [ "buildConfig" "cdnConfig" ] + lib.concatMapStrings (index:
          let file = fetchCdn index;
          in ''
            ln -s ${file} "$out/${index.archive}.index"
          '') (lock.bootstrap.archives or []));
    in cache;
  mkStorage = { storageLockFile, hash ? null }:
    let
      lock = builtins.fromJSON (builtins.readFile storageLockFile);
      cache = mkObjectCache lock;
    in assert lock.schemaVersion == 1 && lock.storageFormat == "casc-v7";
       assert !(lock.completeInstallation or false);
    runCommand "battlenet-${lock.release.product}-${lock.release.buildId or "sample"}-partial-storage"
      ({ nativeBuildInputs = [ python3 ]; meta.license = lib.licenses.unfree; }
        // lib.optionalAttrs (hash != null) {
          outputHashMode = "recursive";
          outputHashAlgo = "sha256";
          outputHash = hash;
        }) ''
      python3 ${source}/ngdp.py materialize-storage ${storageLockFile} \
        --cache ${cache} --output "$TMPDIR/storage"
      cp -r "$TMPDIR/storage" "$out"
    '';
  mkInstallation = { storageLockFile, storageLock ? null, hash ? null }:
    let
      lock = if storageLock != null then storageLock else builtins.fromJSON (builtins.readFile storageLockFile);
      cache = mkObjectCache lock;
    in assert lock.completeInstallation && lock.storageFormat == "casc-v7";
    runCommand "battlenet-${lock.release.product}-${lock.release.buildId}-installation" {
      nativeBuildInputs = [ python3 ];
      # Pure assembly: all network inputs are individually hash-pinned.
      # The launcher retains these inputs so GC does not discard the update cache.
      passthru.objectCache = cache;
      meta.license = lib.licenses.unfree;
    } ''
      python3 ${source}/ngdp.py materialize-storage ${storageLockFile} \
        --cache ${cache} --output "$TMPDIR/storage"
      mv "$TMPDIR/storage" "$out"
    '';
  mkSnapshot = { src, lockFile }:
    let lock = readLock lockFile;
    in runCommand "battlenet-${lock.product}-${lock.buildId}-content" {
      nativeBuildInputs = [ python3 ];
      meta.license = lib.licenses.unfree;
    } ''
      python3 ${source}/ngdp.py verify-install ${lib.escapeShellArg (toString src)} ${lockFile}
      mkdir -p "$out"
      cp -r ${lib.escapeShellArg (toString src)}/. "$out/"
    '';
  mkConfiguredGame = { content ? null, storageLockFile ? null, storageLock ? null, hash ? null, ... }@settings:
    assert lib.assertMsg ((content != null) != (storageLockFile != null))
      "A game needs exactly one of content or storageLockFile.";
    let
      release = readLock settings.lockFile;
      storageRelease = if storageLockFile == null then null else
        (if storageLock != null then storageLock else builtins.fromJSON (builtins.readFile storageLockFile)).release;
      selectedContent = if content != null then content else
        mkInstallation { inherit storageLockFile storageLock hash; };
    in assert lib.assertMsg (storageRelease == null ||
      (release.product == storageRelease.product &&
       release.buildConfig.key == storageRelease.buildConfig.key &&
       release.cdnConfig.key == storageRelease.cdnConfig.key))
      "Release and storage locks must refer to the same product and build.";
    mkGame ((builtins.removeAttrs settings [ "content" "storageLockFile" "storageLock" "hash" ]) // {
      content = selectedContent;
    });
  mkCatalogGame = { release, installation ? null, content ? null, executable, args ? [], persistentDirectories ? null, locale ? null, region ? null }:
    assert lib.assertMsg (!darwin || content != null ||
      (installation != null && builtins.elem "OSX" installation.storage.tags))
      "Darwin needs a verified native macOS installation.";
    assert lib.assertMsg (locale == null || content != null || (installation != null && builtins.elem locale installation.storage.tags))
      "The installation does not contain the requested language.";
    mkConfiguredGame {
      lockFile = builtins.toFile "battlenet-${release.product}-release.json" (builtins.toJSON release);
      storageLockFile = if installation == null then null else
        builtins.toFile "battlenet-${release.product}-storage.json" (builtins.toJSON installation.storage);
      # Keep the native table; avoid serializing and reparsing millions of objects.
      storageLock = if installation == null then null else installation.storage;
      hash = if installation == null then null else installation.hash or null;
      inherit content executable args locale region;
      workingDirectory = if darwin then (if installation == null then "." else installation.storage.installDirectory or ".") else null;
      persistentDirectories = if persistentDirectories != null then persistentDirectories
        else if builtins.elem release.product [ "wow" "wow_classic" "wow_classic_era" ] then
          let folder = if darwin then (if installation == null then "." else installation.storage.installDirectory or ".") else builtins.dirOf executable;
          in map (name: if folder == "." then name else "${folder}/${name}") [ "WTF" "Interface" "Screenshots" ]
        else if darwin && release.product == "s1" then [ "Maps/save" "Maps/replays" ]
        else [];
    };
  mkGames = games:
    let
      productOf = game: if game ? release then game.release.product else (readLock game.lockFile).product;
      products = lib.mapAttrsToList (_: game: productOf game) games;
    in assert lib.assertMsg (lib.length products == lib.length (lib.unique products))
      "Configured games must use distinct product codes (launcher names are product-based).";
    lib.mapAttrs (_: game: if game ? release then mkCatalogGame game else mkConfiguredGame game) games;
  mkGame = { lockFile, content, executable, args ? [], persistentDirectories ? [], locale ? null, region ? null, workingDirectory ? null }:
    let
        wine = runtime.wine or null;
        dxvk = runtime.dxvk or null;
        lock = readLock lockFile;
        wow = builtins.elem lock.product [ "wow" "wow_classic" "wow_classic_era" ];
        china = region == "cn" && lock.region == "cn";
        variablesFolder = { s2 = "StarCraft II"; hero = "Heroes of the Storm"; }.${lock.product} or null;
        stripLocale = arguments: if arguments == [] then [] else
          if builtins.head arguments == "-locale" then stripLocale (lib.drop 2 arguments)
          else if lib.hasPrefix "-locale=" (builtins.head arguments) then stripLocale (builtins.tail arguments)
          else [ (builtins.head arguments) ] ++ stripLocale (builtins.tail arguments);
        launchArgs = if locale != null && lock.product == "w3" then stripLocale args ++ [ "-locale" locale ] else args;
        clientSettings = lib.optional (locale != null && variablesFolder != null) {
          location = "documents"; path = "${variablesFolder}/Variables.txt"; syntax = "assign";
          values = { localeidassets = locale; localeiddata = locale; };
        } ++ lib.optional (wow && (locale != null || region != null)) {
          location = "game"; path = "${if darwin then (if workingDirectory == null then "." else workingDirectory) else builtins.dirOf executable}/WTF/Config.wtf"; syntax = "wtf";
          values = lib.optionalAttrs (locale != null) { inherit locale; }
            // lib.optionalAttrs (region != null) { portal = lib.toUpper region; };
        };
        settingsFile = builtins.toFile "battlenet-${lock.product}-settings.json" (builtins.toJSON clientSettings);
        generation = "${lock.product}${lib.optionalString (region == "cn") "-cn"}-${lock.buildConfig.key}-${lock.cdnConfig.key}-${builtins.baseNameOf (toString content)}";
        nativeSettings = writeText "battlenet-${lock.product}-native-runtime.json" (builtins.toJSON {
          content = toString content;
          release = toString lockFile;
          inherit executable generation persistentDirectories clientSettings;
          workingDirectory = if workingDirectory == null then "." else workingDirectory;
          args = launchArgs;
        });
    in assert lib.assertMsg (region != "cn" || lock.region == "cn")
      "The China service requires a verified China release.";
    assert lib.assertMsg (locale == null || builtins.match "[a-z]{2}[A-Z]{2}" locale != null)
      "Language must be a locale such as enUS or zhCN.";
    assert lib.assertMsg (region == null || (wow && builtins.elem region [ "us" "eu" "cn" ]) ||
      china)
      "Service region preferences support WoW us/eu/cn. Other games require a verified China distribution for cn and use their own login flow.";
    assert executable != "" && !(lib.hasPrefix "/" executable)
      && !(builtins.elem ".." (lib.splitString "/" executable));
    assert lib.assertMsg (!darwin || lib.hasInfix ".app/Contents/MacOS/" executable)
      "Darwin launches a native application executable, not a Windows client.";
    ((if darwin then writeShellApplication {
      name = lock.product;
      runtimeInputs = [ python3 ];
      text = ''
        exec python3 ${source}/runtime.py run-native ${nativeSettings} "$@"
      '';
      meta = {
        description = "Native macOS runtime for Battle.net product ${lock.product}";
        platforms = [ "aarch64-darwin" ];
        license = lib.licenses.mit;
      };
    } else writeShellApplication {
      name = lock.product;
      runtimeInputs = [ python3 wine util-linux ];
      text = ''
        state="''${XDG_STATE_HOME:-$HOME/.local/state}/battlenet/${generation}"
        mkdir -p "$state"
        exec 9>"$state/runtime.lock"
        flock -n 9 || { echo "This build is already running" >&2; exit 1; }
        mkdir -p "$state/prefix"
        python3 ${source}/ngdp.py verify-install ${content} ${lockFile}
        export WINEPREFIX="$state/prefix"
        session=$(mktemp -d "$state/session.XXXXXXXX")
        cleanup() {
          wineserver -w || true
          cd "$state"
          rm -rf -- "$session"
        }
        trap cleanup EXIT
        python3 ${source}/runtime.py prepare-game ${content} "$session/game"
        ${lib.optionalString (persistentDirectories != []) ''
          python3 ${source}/runtime.py persist-directories "$session/game" "$state/game-state" ${lib.escapeShellArgs persistentDirectories}
        ''}
        if [[ ! -f "$WINEPREFIX/.battlenet-home-isolated" ]]; then
          WINEDLLOVERRIDES="''${WINEDLLOVERRIDES:+$WINEDLLOVERRIDES;}mscoree,mshtml=" wine wineboot.exe --init 9>&-
          wineserver -w
          python3 ${source}/runtime.py "$WINEPREFIX"
          touch "$WINEPREFIX/.battlenet-home-isolated"
        fi
        ${lib.optionalString (clientSettings != []) ''
          python3 ${source}/runtime.py apply-settings "$WINEPREFIX" "$session/game" ${settingsFile}
        ''}
        ${lib.optionalString (dxvk != null) ''
          for dll in d3d11 dxgi; do
            ln -sf ${dxvk}/x64/"$dll.dll" "$WINEPREFIX/drive_c/windows/system32/$dll.dll"
            ln -sf ${dxvk}/x32/"$dll.dll" "$WINEPREFIX/drive_c/windows/syswow64/$dll.dll"
          done
          export WINEDLLOVERRIDES="''${WINEDLLOVERRIDES:+$WINEDLLOVERRIDES;}d3d11,dxgi=n,b"
        ''}
        cd "$session/game"/${lib.escapeShellArg (builtins.dirOf executable)}
        wine ${lib.escapeShellArg (builtins.baseNameOf executable)} ${lib.escapeShellArgs launchArgs} "$@" 9>&-
      '';
      meta = {
        description = "Version-isolated Wine runtime for Battle.net product ${lock.product}";
        platforms = [ "x86_64-linux" ];
        license = lib.licenses.mit;
      };
    })).overrideAttrs (old: {
      buildCommand = old.buildCommand + lib.optionalString (content ? objectCache) ''
        mkdir -p "$out/share/battlenet"
        ln -s ${content.objectCache} "$out/share/battlenet/objects"
      '';
    });
in {
  inherit runtime readLock mkMetadata mkManifests mkPlan mkFile mkEncodedObject mkStorage mkInstallation mkSnapshot mkGame mkConfiguredGame mkCatalogGame mkGames;
}
