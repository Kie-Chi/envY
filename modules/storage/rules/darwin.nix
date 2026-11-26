{ home }:

{
  envy-swift-module-cache = {
    path = "${home}/.cache/envy";
    names = [ "swift-module-cache" ];
    targets = "dirs";
    depth = 1;
    recursiveAge = true;
    action = "quarantine";
    prune = true;
    minAgeDays = 7;
    optionalRoot = true;
    processes = [ "swift" "swiftc" "swift-frontend" "clang" "clang[+][+]" ];
  };
  feishu-web-cache-aha = {
    action = "quarantine";
    depth = 1;
    extensions = [  ];
    minAgeDays = 14;
    minSizeBytes = 0;
    names = [  ];
    path = "${home}/Library/Application Support/LarkShell/aha/users/*/profile_explorer/Service Worker/CacheStorage";
    processes = [ "Feishu" "Lark" ];
    recursiveAge = true;
    targets = "dirs";
    optionalRoot = true;
    protectedOverrides = [ "${home}/Library/Application Support/LarkShell/aha/users/*/profile_explorer/Service Worker/CacheStorage" ];
  };
  feishu-web-cache-iron = {
    action = "quarantine";
    depth = 1;
    extensions = [  ];
    minAgeDays = 14;
    minSizeBytes = 0;
    names = [  ];
    path = "${home}/Library/Application Support/LarkShell/iron/users/*/profile_main/Service Worker/CacheStorage";
    processes = [ "Feishu" "Lark" ];
    recursiveAge = true;
    targets = "dirs";
    optionalRoot = true;
    protectedOverrides = [ "${home}/Library/Application Support/LarkShell/iron/users/*/profile_main/Service Worker/CacheStorage" ];
  };
  feishu-update = {
    action = "quarantine";
    depth = 1;
    extensions = [  ];
    minAgeDays = 14;
    minSizeBytes = 0;
    names = [ "update.noindex" "update_downloading" ];
    path = "${home}/Library/Application Support/LarkShell/update";
    processes = [
      "Feishu" "Lark"
      "/(Feishu|Lark)[.]app/"
      "/LarkShell/update/darwin[-_]updater"
    ];
    recursiveAge = true;
    targets = "dirs";
    optionalRoot = true;
    protectedOverrides = [
      "${home}/Library/Application Support/LarkShell/update/update.noindex"
      "${home}/Library/Application Support/LarkShell/update/update_downloading"
    ];
  };
  feishu-logs = {
    action = "quarantine";
    depth = null;
    extensions = [ "log" "alaudalog" ];
    minAgeDays = 30;
    minSizeBytes = 0;
    names = [  ];
    path = "${home}/Library/Application Support/LarkShell/sdk_storage/log";
    processes = [  ];
    recursiveAge = false;
    targets = "files";
    optionalRoot = true;
    protectedOverrides = [ "${home}/Library/Application Support/LarkShell/sdk_storage/log" ];
  };
  codex-sparkle = {
    action = "quarantine";
    depth = 1;
    extensions = [  ];
    minAgeDays = 14;
    minSizeBytes = 0;
    names = [  ];
    path = "${home}/Library/Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation";
    processes = [
      "Codex" "ChatGPT"
      "/(Codex|ChatGPT)[.]app/"
      "/com[.]openai[.]codex/org[.]sparkle-project[.]Sparkle/"
    ];
    recursiveAge = true;
    targets = "dirs";
    optionalRoot = true;
  };
  vscode-shipit = {
    action = "quarantine";
    depth = 1;
    extensions = [  ];
    minAgeDays = 14;
    minSizeBytes = 0;
    names = [ "Visual Studio Code.app" ];
    path = "${home}/Library/Caches/com.microsoft.VSCode.ShipIt/update.*";
    processes = [
      "/Visual Studio Code[.]app/"
      "/com[.]microsoft[.]VSCode[.]ShipIt/"
    ];
    recursiveAge = true;
    targets = "dirs";
    optionalRoot = true;
  };
  wechat-sparkle = {
    path = "${home}/Library/Caches/com.tencent.xinWeChat/org.sparkle-project.Sparkle/Installation";
    targets = "files";
    depth = 2;
    extensions = [ "dmg" ];
    action = "quarantine";
    minAgeDays = 14;
    optionalRoot = true;
    processes = [
      "WeChat" "WeChatAppEx"
      "/WeChat[.]app/"
      "/com[.]tencent[.]xinWeChat/org[.]sparkle-project[.]Sparkle/"
    ];
  };
  zotero-update-archive = {
    path = "${home}/Library/Caches/Zotero/updates/Applications/Zotero/updates/0";
    targets = "files";
    depth = 1;
    names = [ "update.mar" ];
    action = "quarantine";
    minAgeDays = 14;
    optionalRoot = true;
    processes = [ "zotero" "Zotero" "/Zotero[.]app/" "/Zotero/updates/" ];
  };
  zotero-update-staging = {
    path = "${home}/Library/Caches/Zotero/updates/Applications/Zotero/updates/0";
    targets = "dirs";
    depth = 1;
    names = [ "Updated.app" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 14;
    optionalRoot = true;
    processes = [ "zotero" "Zotero" "/Zotero[.]app/" "/Zotero/updates/" ];
  };
  go-build-cache = {
    action = "quarantine";
    depth = null;
    extensions = [  ];
    minAgeDays = 30;
    minSizeBytes = 0;
    names = [  ];
    path = "${home}/Library/Caches/go-build";
    processes = [  ];
    recursiveAge = false;
    targets = "files";
    optionalRoot = true;
  };
  wechat-cache-dirs = {
    action = "quarantine";
    depth = 1;
    extensions = [  ];
    minAgeDays = 14;
    minSizeBytes = 0;
    names = [  ];
    path = "${home}/Library/Containers/com.tencent.xinWeChat/Data/Library/Caches";
    processes = [ "WeChat" "WeChatAppEx" ];
    recursiveAge = true;
    targets = "dirs";
    optionalRoot = true;
    protectedOverrides = [ "${home}/Library/Containers/com.tencent.xinWeChat/Data/Library/Caches" ];
  };
  wechat-cache-files = {
    action = "quarantine";
    depth = null;
    extensions = [  ];
    minAgeDays = 14;
    minSizeBytes = 1048576;
    names = [  ];
    path = "${home}/Library/Containers/com.tencent.xinWeChat/Data/Library/Caches";
    processes = [ "WeChat" "WeChatAppEx" ];
    recursiveAge = false;
    targets = "files";
    optionalRoot = true;
    protectedOverrides = [ "${home}/Library/Containers/com.tencent.xinWeChat/Data/Library/Caches" ];
  };
  wechat-web-cache = {
    action = "quarantine";
    depth = 1;
    extensions = [  ];
    minAgeDays = 14;
    minSizeBytes = 0;
    names = [ "Cache" ];
    path = "${home}/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles/*";
    processes = [ "WeChat" "WeChatAppEx" ];
    recursiveAge = true;
    targets = "dirs";
    optionalRoot = true;
    protectedOverrides = [ "${home}/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles/*/Cache" ];
  };
  wechat-web-cachestorage = {
    action = "quarantine";
    depth = 1;
    extensions = [  ];
    minAgeDays = 14;
    minSizeBytes = 0;
    names = [ "CacheStorage" ];
    path = "${home}/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles/*/Service Worker";
    processes = [ "WeChat" "WeChatAppEx" ];
    recursiveAge = true;
    targets = "dirs";
    optionalRoot = true;
    protectedOverrides = [ "${home}/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles/*/Service Worker/CacheStorage" ];
  };
  wechat-logs = {
    action = "quarantine";
    depth = null;
    extensions = [ "xlog" ];
    minAgeDays = 30;
    minSizeBytes = 0;
    names = [  ];
    path = "${home}/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log";
    processes = [  ];
    recursiveAge = false;
    targets = "files";
    optionalRoot = true;
    protectedOverrides = [ "${home}/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log" ];
  };
  vscode-vsix = {
    path = "${home}/Library/Application Support/Code/CachedExtensionVSIXs";
    targets = "files";
    depth = 1;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Visual Studio Code[.]app/" ];
    protectedOverrides = [ "${home}/Library/Application Support/Code/CachedExtensionVSIXs" ];
  };
  vscode-vsix-trash = {
    path = "${home}/Library/Application Support/Code/CachedExtensionVSIXs/.trash";
    targets = "files";
    depth = 1;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Visual Studio Code[.]app/" ];
    protectedOverrides = [ "${home}/Library/Application Support/Code/CachedExtensionVSIXs/.trash" ];
  };
  vscode-cache = {
    path = "${home}/Library/Application Support/Code/Cache";
    targets = "dirs";
    depth = 1;
    names = [ "Cache_Data" "No_Vary_Search" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Visual Studio Code[.]app/" ];
    protectedOverrides = [ "${home}/Library/Application Support/Code/Cache" ];
  };
  vscode-cached-data = {
    path = "${home}/Library/Application Support/Code/CachedData";
    targets = "dirs";
    depth = 1;
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Visual Studio Code[.]app/" ];
    protectedOverrides = [ "${home}/Library/Application Support/Code/CachedData" ];
  };
  vscode-logs = {
    path = "${home}/Library/Application Support/Code/logs";
    targets = "dirs";
    depth = 1;
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Visual Studio Code[.]app/" ];
    protectedOverrides = [ "${home}/Library/Application Support/Code/logs" ];
  };
  chrome-cache = {
    path = "${home}/Library/Caches/Google/Chrome/*/Cache";
    targets = "dirs";
    depth = 1;
    names = [ "Cache_Data" "No_Vary_Search" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Google Chrome[.]app/" ];
  };
  chrome-code-cache = {
    path = "${home}/Library/Caches/Google/Chrome/*/Code Cache";
    targets = "dirs";
    depth = 1;
    names = [ "js" "wasm" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Google Chrome[.]app/" ];
  };
  vscode-gpu-cache = {
    prune = true;
    path = "${home}/Library/Application Support/Code";
    targets = "dirs";
    depth = 1;
    names = [ "GPUCache" "DawnGraphiteCache" "DawnWebGPUCache" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Visual Studio Code[.]app/" ];
    protectedOverrides = [
      "${home}/Library/Application Support/Code/GPUCache"
      "${home}/Library/Application Support/Code/DawnGraphiteCache"
      "${home}/Library/Application Support/Code/DawnWebGPUCache"
    ];
  };
  codex-shader-cache = {
    prune = true;
    path = "${home}/Library/Application Support/Codex";
    targets = "dirs";
    depth = 1;
    names = [ "GraphiteDawnCache" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/ChatGPT[.]app/" "/Codex[.]app/" ];
    protectedOverrides = [ "${home}/Library/Application Support/Codex/GraphiteDawnCache" ];
  };
  chrome-shader-cache = {
    prune = true;
    path = "${home}/Library/Application Support/Google/Chrome";
    targets = "dirs";
    depth = 1;
    names = [ "GrShaderCache" "GraphiteDawnCache" "ShaderCache" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Google Chrome[.]app/" ];
    protectedOverrides = [
      "${home}/Library/Application Support/Google/Chrome/GrShaderCache"
      "${home}/Library/Application Support/Google/Chrome/GraphiteDawnCache"
      "${home}/Library/Application Support/Google/Chrome/ShaderCache"
    ];
  };
  feishu-shader-cache = {
    prune = true;
    path = "${home}/Library/Application Support/LarkShell";
    targets = "dirs";
    depth = 1;
    names = [ "GrShaderCache" "GraphiteDawnCache" "ShaderCache" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Feishu[.]app/" "/Lark[.]app/" ];
    protectedOverrides = [
      "${home}/Library/Application Support/LarkShell/GrShaderCache"
      "${home}/Library/Application Support/LarkShell/GraphiteDawnCache"
      "${home}/Library/Application Support/LarkShell/ShaderCache"
    ];
  };
  feishu-iron-shader-cache = {
    prune = true;
    path = "${home}/Library/Application Support/LarkShell/iron";
    targets = "dirs";
    depth = 1;
    names = [ "GrShaderCache" "GraphiteDawnCache" "ShaderCache" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Feishu[.]app/" "/Lark[.]app/" ];
    protectedOverrides = [
      "${home}/Library/Application Support/LarkShell/iron/GrShaderCache"
      "${home}/Library/Application Support/LarkShell/iron/GraphiteDawnCache"
      "${home}/Library/Application Support/LarkShell/iron/ShaderCache"
    ];
  };
  steam-shader-cache = {
    prune = true;
    path = "${home}/Library/Application Support/Steam/config/htmlcache";
    targets = "dirs";
    depth = 1;
    names = [ "GrShaderCache" "GraphiteDawnCache" "ShaderCache" ];
    recursiveAge = true;
    action = "quarantine";
    minAgeDays = 30;
    optionalRoot = true;
    processes = [ "/Steam[.]AppBundle/" "steam_osx" "steamwebhelper" ];
    protectedOverrides = [
      "${home}/Library/Application Support/Steam/config/htmlcache/GrShaderCache"
      "${home}/Library/Application Support/Steam/config/htmlcache/GraphiteDawnCache"
      "${home}/Library/Application Support/Steam/config/htmlcache/ShaderCache"
    ];
  };
}
