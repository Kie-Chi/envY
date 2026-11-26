{ lib, pkgs, package, electron }:

let
  darwinInfo = {
    CFBundleIdentifier = "com.t3tools.t3code";
    CFBundleShortVersionString = lib.head (lib.splitString "-" package.version);
    CFBundleVersion = lib.head (lib.splitString "-" package.version);
    LSApplicationCategoryType = "public.app-category.developer-tools";
    NSAutoFillRequiresTextContentTypeForOneTimeCodeOnMac = true;
    NSLocalNetworkUsageDescription = "T3 Code connects to devices on your local network for remote environments and commands run by terminals and coding agents.";
    NSCalendarsUsageDescription = "Commands you run in T3 Code may need access to your calendars.";
    NSCalendarsFullAccessUsageDescription = "Commands you run in T3 Code may need to read and write your calendar events.";
    NSCalendarsWriteOnlyAccessUsageDescription = "Commands you run in T3 Code may need to add events to your calendars.";
    NSScreenCaptureUsageDescription = "T3 Code captures the active window when you use the window capture shortcut.";
    NSCameraUsageDescription = "T3 Code lets commands run by terminals and coding agents use the camera.";
    NSMicrophoneUsageDescription = "T3 Code lets commands run by terminals and coding agents use the microphone.";
    NSSpeechRecognitionUsageDescription = "T3 Code transcribes your dictation on this Mac when you choose Apple Speech.";
    CFBundleDocumentTypes = [{
      CFBundleTypeName = "Web page";
      CFBundleTypeRole = "Viewer";
      LSHandlerRank = "Alternate";
      LSItemContentTypes = [ "public.html" "public.xhtml" ];
    }];
    CFBundleURLTypes = [
      { CFBundleURLName = "T3 Code"; CFBundleURLSchemes = [ "t3code" "t3code-dev" ]; }
      { CFBundleURLName = "Web site URL"; CFBundleURLSchemes = [ "http" "https" ]; CFBundleTypeRole = "Viewer"; }
    ];
  };
  unwrapped = package.unwrapped.overrideAttrs (old: {
    patches = (old.patches or []) ++ [
      ../../../../files/agents/patches/t3code-pi-goal-tasks.patch
      ../../../../files/agents/patches/t3code-pi-queue-messages.patch
      ../../../../files/agents/patches/t3code-desktop-name.patch
      ../../../../files/agents/patches/t3code-nix-service.patch
    ];
    patchFlags = [ "-p1" "--fuzz=0" ];
    preBuild = (old.preBuild or "") + ''
      # ChatView relies on compiler memoization for its render-time state guards.
      (
        cd apps/web
        pnpm exec vp test run src/reactCompilerCoverage.test.ts --project unit --reporter=dot \
          -t "ChatView|QueueMessageList|QueueMessageControl|ThreadActivityBanners|GoalTaskList"
      )
      # Queue controls cross the wire and must typecheck on both sides.
      for project in packages/contracts packages/provider-core packages/provider-pi packages/client-runtime apps/server apps/web; do
        pnpm exec effect-tsgo tsrs --noEmit -p "$project/tsconfig.json"
      done
    '';
    postBuild = (old.postBuild or "") + ''
      ${pkgs.python3.interpreter} ${./verify_build.py} apps/server/dist
    '';
    doCheck = true;
    checkPhase = ''
      runHook preCheck
      export PYTHONDONTWRITEBYTECODE=1
      T3CODE_VERIFY_BUILD=${./verify_build.py} ${pkgs.python3.interpreter} -m unittest discover -s ${./tests} -p 'test_*.py'
      (
        cd apps/server
        # Demonstrate the regression against upstream in this isolated build tree.
        cp src/cloud/bootService.ts "$TMPDIR/bootService.patched.ts"
        cp ${package.unwrapped.src}/apps/server/src/cloud/bootService.ts src/cloud/bootService.ts
        redStatus=0
        pnpm exec vp test run --config ../../vite.config.ts src/cloud/bootService.test.ts \
          -t "recognizes darwin Nix generations without pinned runtime state" \
          > "$TMPDIR/nix-service-red.log" 2>&1 || redStatus=$?
        cp "$TMPDIR/bootService.patched.ts" src/cloud/bootService.ts
        if [ "$redStatus" -eq 0 ] || ! grep -q "AssertionError" "$TMPDIR/nix-service-red.log"; then
          cat "$TMPDIR/nix-service-red.log"
          echo "The unpatched Nix service regression did not fail as expected." >&2
          exit 1
        fi
        echo "Unpatched Nix service regression failed as expected."
        pnpm exec vp test run --config ../../vite.config.ts src/cloud/bootService.test.ts src/cli/service.test.ts src/cli/update.test.ts src/cloud/selfUpdate.test.ts src/auth/RpcAuthorization.test.ts src/observability/RpcInstrumentation.test.ts
      )
      (
        cd packages/provider-pi
        pnpm exec vp test run --config ../../vite.config.ts src/server/adapter.test.ts
      )
      (
        cd packages/client-runtime
        pnpm exec vp test run --config ../../vite.config.ts src/state/commandPermissions.test.ts
      )
      (
        cd apps/web
        pnpm exec vp test run src/components/chat/GoalTaskList.test.tsx src/components/chat/QueueMessageList.test.tsx --project unit
      )
      (
        cd apps/desktop
        # Resolve the Electron import through Nix; the tests supply the app API.
        ELECTRON_OVERRIDE_DIST_PATH=${electron}/bin pnpm exec vp test run --config ../../vite.config.ts src/app/DesktopAppIdentity.test.ts src/app/DesktopEnvironment.test.ts
      )
      runHook postCheck
    '';
  });
  # Keep the upstream runtime PATH and resource monitor around the native bundle.
  darwinLauncher = pkgs.writeShellScript "t3code-desktop" ''
    if [ -n "''${ENVY_T3_NATIVE_EXECUTABLE:-}" ]; then
      exec "$ENVY_T3_NATIVE_EXECUTABLE" "${unwrapped}/libexec/t3code" "$@"
    fi
    exec ${unwrapped}/bin/t3code-desktop "$@"
  '';
  desktopBase = if pkgs.stdenv.hostPlatform.isDarwin then pkgs.symlinkJoin {
    pname = "t3code-native-launcher";
    inherit (unwrapped) version meta passthru src;
    paths = [ unwrapped ];
    postBuild = ''
      rm "$out/bin/t3code-desktop"
      ln -s ${darwinLauncher} "$out/bin/t3code-desktop"
    '';
  } else unwrapped;
  wrapped = package.override { t3code-unwrapped = desktopBase; };
in
wrapped.overrideAttrs (old: {
  nativeBuildInputs = (old.nativeBuildInputs or [])
    ++ lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.rcodesign ];
  # symlinkJoin embeds postBuild in buildCommand before overrideAttrs runs.
  buildCommand = old.buildCommand + ''
    wrapProgram "$out/bin/t3" --set T3CODE_NIX_PACKAGE "$out"
    wrapProgram "$out/bin/t3code-desktop" --unset ELECTRON_RUN_AS_NODE --set T3CODE_DISABLE_AUTO_UPDATE 1 --set ELECTRON_FORCE_IS_PACKAGED true
    if [ -d "$out/share/applications" ]; then
      for desktop in "$out"/share/applications/*.desktop; do
        cp -L "$desktop" "$desktop.copy"
        mv -f "$desktop.copy" "$desktop"
        sed -i 's/^Name=.*/Name=T3 Code/' "$desktop"
      done
    fi
  '' + lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
    rm -rf "$out/Applications"
    mkdir -p "$out/Applications"
    app="$out/Applications/T3 Code.app"
    icons=("${unwrapped}"/Applications/*.app/Contents/Resources/t3code.icns)
    ${pkgs.python3.interpreter} ${./bundle.py} "${electron}/Applications/Electron.app" "$app" ${pkgs.writeText "t3code-darwin-info.json" (builtins.toJSON darwinInfo)} "''${icons[0]}"
    native="$app/Contents/MacOS/T3 Code"
    wrapProgram "$out/bin/t3code-desktop" --set ENVY_T3_NATIVE_EXECUTABLE "$native" --set T3CODE_DESKTOP_RESOURCES_PATH "${unwrapped}/libexec/t3code/apps/desktop/prod-resources"
    makeWrapper "$out/bin/t3code-desktop" "$app/Contents/MacOS/T3 Code Launcher"
    rcodesign sign --binary-identifier com.t3tools.t3code "$native"
    rcodesign sign "$app"
  '';
})
